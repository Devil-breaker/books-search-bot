"""GoodreadsBot — all Telegram command and callback handlers."""

import asyncio
import copy
import hashlib
import json
import os
import re
import threading
import tempfile
import time
import requests
import unicodedata
from typing import Any
from html import unescape as html_unescape
from src.admins import MongoBotAdminRepository
from src.channel_connections import MongoChannelConnectionRepository
from src.channel_index import ChannelIndexRuntime, MongoChannelIndexRepository
from src.channel_management import ChannelManager
from src.miniapp.auth import issue_inline_token
from io import BytesIO
from PIL import Image
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from telegram import (
    Update,
    WebAppInfo,
    InlineQueryResultsButton,
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeAllGroupChats,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InputMediaPhoto,
    ReplyParameters,
)
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, InlineQueryHandler, MessageHandler, ContextTypes, filters
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, TimedOut

from src.utils import (
    logger, HEADERS, get_http_session, html_escape, is_placeholder_image,
    is_english_description, translate_to_english,
)
from src.search import build_goodreads_url
from src.aggregator import MultiSourceBookAggregator, GOOGLE_BOOKS_API_KEY


class GoodreadsBot:
    def __init__(self, token: str, webhook_mode: bool = False):
        self.token = token
        # Used by webhook deployments to reject commands Telegram redelivers
        # from before this process started. Polling also drops its backlog.
        self._started_at = time.time()
        # Generous timeouts make the long-polling loop more tolerant of slow or
        # flaky networks (the source of the httpx.ReadError on getUpdates).
        self.app = (
            Application.builder()
            .token(token)
            .connect_timeout(30.0)
            .read_timeout(30.0)
            .write_timeout(30.0)
            .pool_timeout(30.0)
            .get_updates_connect_timeout(30.0)
            .get_updates_read_timeout(40.0)
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
            .build()
        )
        # search_cache: {user_id: (books_list, timestamp)}
        # Entries expire after _SEARCH_CACHE_TTL seconds.
        self.search_cache: dict = {}
        self._SEARCH_CACHE_TTL: int = 60 * 60  # 60 minutes
        # Per-user current page and query text for normal search pagination
        self._search_page_cache: dict[int, int] = {}  # user_id -> page number
        self._search_query_cache: dict[int, str] = {}  # user_id -> query text
        # Immutable-per-search result snapshots, addressed by the Telegram
        # message that owns each set of numbered buttons.
        self._result_message_cache: dict[tuple[int, int], dict] = {}
        self._RESULT_MESSAGE_CACHE_TTL: int = 60 * 60
        self._RESULT_MESSAGE_CACHE_MAX: int = 1000
        self._SEARCH_CACHE_MAX: int = 1000      # max users tracked
        self.aggregator = MultiSourceBookAggregator()
        self.webhook_mode = webhook_mode
        # Per-user inline query debounce tasks.
        # Key = user_id; Value = asyncio.Task that performs the debounced search.
        self._inline_debounce_tasks: dict[int, asyncio.Task] = {}
        self._inline_debounce_lock = asyncio.Lock()
        # Inline callback cache: {callback_key: book_data}
        # Entries expire after _INLINE_CALLBACK_CACHE_TTL seconds.
        self._inline_callback_cache: dict = {}
        self._INLINE_CALLBACK_CACHE_TTL: int = 30 * 60  # 30 minutes
        # Short-lived shared cache avoids repeating provider requests for the same query.
        self._aggregate_search_cache: dict[str, tuple[float, list[dict]]] = {}
        self._AGGREGATE_SEARCH_CACHE_TTL: int = 120
        self._AGGREGATE_SEARCH_CACHE_MAX: int = 128
        self._aggregate_search_inflight: dict[str, asyncio.Task] = {}
        # Clarification state: {user_id: (original_query, title_hint, author_hint)}
        self._clarification: dict = {}
        # Rate-limit: {(user_id, norm_query): timestamp}
        self._clarification_rate_limit: dict = {}
        self._clarification_discovery_cache: dict = {}
        self._clarification_discovery_inflight: dict = {}
        # Suppress repeated cancellation follow-up messages per user.
        self._clarification_cancel_notice_rate_limit: dict[int, float] = {}
        # Escalating abuse controls for repeated clarification cancellations.
        self._clarification_cancel_abuse: dict[int, dict] = {}
        self._clarification_abuse_notice_rate_limit: dict[int, float] = {}
        self._authorized_bot_admin_ids: set[int] = set()
        self._bot_admin_cache_loaded = False
        self._bot_admin_cache_expires_at = 0.0
        self._bot_admin_cache_lock = asyncio.Lock()
        self._bot_admin_repository: MongoBotAdminRepository | None = None
        self._channel_connection_repository: MongoChannelConnectionRepository | None = None
        self._channel_index_repository: MongoChannelIndexRepository | None = None
        self._channel_index_runtime: ChannelIndexRuntime | None = None
        self._index_pending_input: dict[int, dict] = {}
        # Private command suggestions are presentation-only and refreshed in
        # the background. Keep them cached to avoid Telegram round trips on
        # every /start while still rechecking channel access periodically.
        self._user_command_scope_cache: dict[int, tuple[float, bool]] = {}
        self._user_command_scope_locks: dict[int, asyncio.Lock] = {}
        self._user_command_scope_tasks: set[asyncio.Task] = set()
        # Cache group-admin checks briefly so restriction checks do not add a
        # Telegram API call to every search/cancel interaction.
        self._group_admin_status_cache: dict[tuple[int, int], tuple[float, bool]] = {}
        self._group_search_rate_limit: dict[tuple[int, int], float] = {}
        self._group_search_notice_rate_limit: dict[tuple[int, int], float] = {}
        self._group_search_inflight: set[tuple[int, int]] = set()
        # Current visible results per (chat, requester), used to prevent late
        # rating updates from overwriting a newer page or selected book.
        self._active_result_messages: dict[tuple[int, int], dict] = {}
        self._rating_refresh_tasks: set[asyncio.Task] = set()
        owner_id = os.getenv("BOT_OWNER_ID", "").strip()
        try:
            self._owner_user_id: int | None = int(owner_id) if owner_id else None
        except ValueError:
            self._owner_user_id = None
            logger.warning("BOT_OWNER_ID must be a numeric Telegram user ID; owner exemption is disabled")
        if not owner_id:
            logger.warning("BOT_OWNER_ID is not configured; no user is exempt from cancellation abuse limits")
        self._channel_manager = ChannelManager(
            self._owner_user_id,
            self._get_channel_connection_repository,
            self._open_channel_manager_index,
            self._resolve_channel_admin_username,
            self._delete_channel_messages_mtproto,
        )
        self._channel_manager_task: asyncio.Task | None = None
        self.setup_handlers()

    _CANCEL_ABUSE_THRESHOLD = 3
    _CANCEL_ABUSE_WINDOW_SECONDS = 60
    _CANCEL_ABUSE_COOLDOWN_SECONDS = 5 * 60
    _CANCEL_ABUSE_ESCALATION_WINDOW_SECONDS = 24 * 60 * 60
    _CANCEL_ABUSE_BLOCK_SECONDS = 60 * 60
    _GROUP_SEARCH_COOLDOWN_SECONDS = 3
    _GROUP_SEARCH_NOTICE_INTERVAL_SECONDS = 15
    _CLARIFICATION_DISCOVERY_CACHE_TTL_SECONDS = 5 * 60
    # Negative discoveries can be caused by transient upstream failures or
    # Google Books ranking; retry fairly soon instead of suppressing another
    # clarification attempt for a full minute.
    _CLARIFICATION_DISCOVERY_MISS_TTL_SECONDS = 10
    _CLARIFICATION_DISCOVERY_CACHE_MAX = 256

    _STOPWORDS: set = {
        "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
        "of", "with", "by", "from", "up", "about", "into", "through", "during",
        "is", "are", "was", "were", "be", "been", "being", "have", "has", "had",
        "do", "does", "did", "will", "would", "could", "should", "may", "might",
        "can", "this", "that", "these", "those", "i", "ii", "iii", "iv", "v",
    }

    # ------------------------------------------------------------------
    # Clarification helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_for_matching(text: str) -> str:
        # Lowercase; strip outer punctuation but preserve internal dots (initials).
        return text.lower().strip(" ,;:!?'\"-()[]{}")

    async def _is_owner_or_group_admin(self, user_id: int, chat) -> bool:
        """Return whether the user is privileged or an admin in this group."""
        await self._load_bot_admin_ids()
        if self._is_privileged_bot_user(user_id):
            return True
        if chat is None or getattr(chat, "type", "private") not in ("group", "supergroup"):
            return False
        key = (chat.id, user_id)
        now = time.monotonic()
        cached = self._group_admin_status_cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
        try:
            member = await self.app.bot.get_chat_member(chat.id, user_id)
            is_admin = getattr(member, "status", "") in ("administrator", "creator")
        except Exception as exc:
            logger.debug("Could not verify group admin status for user %s: %s", user_id, exc)
            is_admin = False
        self._group_admin_status_cache[key] = (now + 60, is_admin)
        return is_admin

    def _is_privileged_bot_user(self, user_id: int) -> bool:
        return (
            (getattr(self, "_owner_user_id", None) is not None
             and user_id == self._owner_user_id)
            or user_id in getattr(self, "_authorized_bot_admin_ids", set())
        )

    def _get_bot_admin_repository(self) -> MongoBotAdminRepository | None:
        uri = os.getenv("MONGODB_URI", "").strip()
        if not uri:
            return None
        if getattr(self, "_bot_admin_repository", None) is None:
            database_name = os.getenv("MONGODB_DB_NAME", "annie_db").strip() or "annie_db"
            self._bot_admin_repository = MongoBotAdminRepository(uri, database_name)
        return self._bot_admin_repository

    def _get_channel_connection_repository(self) -> MongoChannelConnectionRepository | None:
        uri = os.getenv("MONGODB_URI", "").strip()
        if not uri:
            return None
        if getattr(self, "_channel_connection_repository", None) is None:
            database_name = os.getenv("MONGODB_DB_NAME", "annie_db").strip() or "annie_db"
            self._channel_connection_repository = MongoChannelConnectionRepository(uri, database_name)
        return self._channel_connection_repository

    async def _open_channel_manager_index(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: int
    ) -> None:
        """Open the existing Index Manager for the channel selected in Channel Manager."""
        await self._select_index_channel(update, context, int(channel_id))

    async def _load_bot_admin_ids(self, force: bool = False) -> bool:
        """Load the small allowlist once; writes update the in-memory copy."""
        lock = getattr(self, "_bot_admin_cache_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._bot_admin_cache_lock = lock
        async with lock:
            return await self._refresh_bot_admin_ids(force)

    async def _refresh_bot_admin_ids(self, force: bool = False) -> bool:
        owner_id = getattr(self, "_owner_user_id", None)
        if owner_id is None:
            self._authorized_bot_admin_ids = set()
            self._bot_admin_cache_loaded = True
            self._bot_admin_cache_expires_at = float("inf")
            return False
        now = time.monotonic()
        if (getattr(self, "_bot_admin_cache_loaded", False) and not force
                and getattr(self, "_bot_admin_cache_expires_at", float("inf")) > now):
            return True
        repository = self._get_bot_admin_repository()
        if repository is None:
            self._bot_admin_cache_loaded = True
            self._bot_admin_cache_expires_at = float("inf")
            return False
        try:
            user_ids = await asyncio.to_thread(repository.list_user_ids)
        except Exception as exc:
            # Keep the owner exempt and fail closed for DB-managed users.
            self._authorized_bot_admin_ids = set()
            self._bot_admin_cache_loaded = True
            self._bot_admin_cache_expires_at = now + 30
            try:
                repository.close()
            except Exception:
                pass
            self._bot_admin_repository = None
            logger.warning("Could not load bot admin allowlist (%s)", type(exc).__name__)
            return False
        if getattr(self, "_owner_user_id", None) is not None:
            user_ids.discard(self._owner_user_id)
        self._authorized_bot_admin_ids = user_ids
        self._bot_admin_cache_loaded = True
        self._bot_admin_cache_expires_at = now + 60
        return True

    async def _group_search_is_rate_limited(self, update: Update) -> bool:
        """Apply a small per-user, per-group cooldown to repeated /search commands."""
        chat = update.effective_chat
        if chat is None or getattr(chat, "type", "private") not in ("group", "supergroup"):
            return False
        user_id = update.effective_user.id
        key = (chat.id, user_id)
        now = time.monotonic()
        last = self._group_search_rate_limit.get(key, 0)
        limited = (
            key in self._group_search_inflight
            or now - last < self._GROUP_SEARCH_COOLDOWN_SECONDS
        )
        if not limited:
            self._group_search_rate_limit[key] = now
            return False
        if await self._is_owner_or_group_admin(user_id, chat):
            self._group_search_rate_limit[key] = now
            return False

        last_notice = self._group_search_notice_rate_limit.get(key, 0)
        if now - last_notice >= self._GROUP_SEARCH_NOTICE_INTERVAL_SECONDS:
            self._group_search_notice_rate_limit[key] = now
            message = update.effective_message
            if message is not None:
                await message.reply_text(
                    "⏳ Please wait a few seconds before searching again.",
                    reply_parameters=ReplyParameters(
                        message_id=message.message_id,
                        allow_sending_without_reply=True,
                    ),
                )
        return True

    async def _discover_candidate_cached(
        self, query: str, title_hint: str | None, author_hint: str | None
    ) -> dict | None:
        """Reuse and coalesce clarification discovery for identical normalized queries."""
        key = (
            self._normalize_for_matching(query),
            self._normalize_for_matching(title_hint or ""),
            self._normalize_for_matching(author_hint or ""),
        )
        now = time.monotonic()
        cache_entry = self._clarification_discovery_cache.get(key)
        if cache_entry and cache_entry[0] > now:
            return copy.deepcopy(cache_entry[1])
        if cache_entry:
            self._clarification_discovery_cache.pop(key, None)

        task = self._clarification_discovery_inflight.get(key)
        if task is None:
            def discover():
                return self._discover_candidate(query, title_hint, author_hint)

            task = asyncio.create_task(asyncio.to_thread(discover))
            self._clarification_discovery_inflight[key] = task

        try:
            candidate = await asyncio.shield(task)
        finally:
            if self._clarification_discovery_inflight.get(key) is task:
                self._clarification_discovery_inflight.pop(key, None)

        ttl = (
            self._CLARIFICATION_DISCOVERY_CACHE_TTL_SECONDS
            if candidate is not None
            else self._CLARIFICATION_DISCOVERY_MISS_TTL_SECONDS
        )
        self._clarification_discovery_cache[key] = (time.monotonic() + ttl, candidate)
        if len(self._clarification_discovery_cache) > self._CLARIFICATION_DISCOVERY_CACHE_MAX:
            oldest = min(
                self._clarification_discovery_cache,
                key=lambda cache_key: self._clarification_discovery_cache[cache_key][0],
            )
            self._clarification_discovery_cache.pop(oldest, None)
        return copy.deepcopy(candidate)

    async def _refresh_result_ratings(
        self, books: list, query_text: str, user_id: int, page_num: int,
        chat_id: int, message_id: int, bot,
    ) -> None:
        """Load list ratings in the background and refresh only the still-active page."""
        try:
            await self._preload_hardcover_ratings_for_page(books, page_num, 5)
            session_key = (chat_id, user_id)
            session = self._active_result_messages.get(session_key)
            if not session or session.get("message_id") != message_id or session.get("page") != page_num:
                return
            text, keyboard = self._build_search_results_message(
                books, query_text, user_id, page_num, 5
            )
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            # The user may already have selected a book, deleted the list, or
            # navigated away while ratings were loading.
            logger.debug("Deferred list rating refresh skipped: %s", exc)

    def _schedule_result_rating_refresh(
        self, books: list, query_text: str, user_id: int, page_num: int,
        chat_id: int, message_id: int, bot,
    ) -> None:
        task = asyncio.create_task(self._refresh_result_ratings(
            books, query_text, user_id, page_num, chat_id, message_id, bot
        ))
        self._rating_refresh_tasks.add(task)
        task.add_done_callback(self._rating_refresh_tasks.discard)

    def _clear_active_result_message(self, session_key: tuple[int, int], message_id: int) -> None:
        session = self._active_result_messages.get(session_key)
        if session and session.get("message_id") == message_id:
            self._active_result_messages.pop(session_key, None)

    def _record_clarification_cancel(self, user_id: int, exempt: bool = False) -> str | None:
        """Count cancel cycles and return an escalation action when a limit is reached."""
        if exempt or self._is_privileged_bot_user(user_id):
            return None
        now = time.time()
        state = self._clarification_cancel_abuse.setdefault(
            user_id,
            {"count": 0, "window_start": now, "level": 0, "escalation_expires": 0},
        )
        if state.get("cooldown_until", 0) > now or state.get("blocked_until", 0) > now:
            return None
        if state.get("level", 0) == 1 and state.get("escalation_expires", 0) <= now:
            state.update(level=0, count=0, window_start=now)
        if now - state.get("window_start", now) > self._CANCEL_ABUSE_WINDOW_SECONDS:
            state["count"] = 0
            state["window_start"] = now
        state["count"] += 1
        if state["count"] < self._CANCEL_ABUSE_THRESHOLD:
            return None
        state["count"] = 0
        state["window_start"] = now
        if state.get("level", 0) == 0:
            state["level"] = 1
            state["cooldown_until"] = now + self._CANCEL_ABUSE_COOLDOWN_SECONDS
            state["escalation_expires"] = now + self._CANCEL_ABUSE_ESCALATION_WINDOW_SECONDS
            logger.warning("User %s reached clarification-cancel threshold; applying 5-minute cooldown", user_id)
            return "cooldown"
        state["level"] = 2
        state["blocked_until"] = now + self._CANCEL_ABUSE_BLOCK_SECONDS
        logger.warning("User %s repeated clarification cancellations after cooldown; blocking searches for 1 hour", user_id)
        return "blocked"

    def _active_clarification_restriction(self, user_id: int) -> tuple[str, int] | None:
        """Return the active restriction and seconds remaining, if any."""
        if self._is_privileged_bot_user(user_id):
            return None
        now = time.time()
        state = self._clarification_cancel_abuse.get(user_id, {})
        blocked_until = state.get("blocked_until", 0)
        if blocked_until > now:
            return "blocked", int(blocked_until - now)
        cooldown_until = state.get("cooldown_until", 0)
        if cooldown_until > now:
            return "cooldown", int(cooldown_until - now)
        return None

    async def _reject_search_during_clarification_restriction(self, update: Update) -> bool:
        user_id = update.effective_user.id
        restriction = self._active_clarification_restriction(user_id)
        if restriction is None:
            return False
        if await self._is_owner_or_group_admin(user_id, update.effective_chat):
            return False
        now = time.time()
        last_notice = self._clarification_abuse_notice_rate_limit.get(user_id, 0)
        if now - last_notice >= 30:
            self._clarification_abuse_notice_rate_limit[user_id] = now
            _, seconds_left = restriction
            minutes_left = max(1, (seconds_left + 59) // 60)
            await update.effective_message.reply_text(
                f"⏳ Searches are temporarily paused after repeated clarification cancellations. "
                f"Please try again in about {minutes_left} minute(s)."
            )
        return True

    def _is_clarification_query(self, query: str) -> tuple[bool, str | None, str | None]:
        # Returns (needs_clarification, title_hint, author_hint)
        # Pattern 1 -- explicit "by":  Title by Author -> always clarify.
        # Pattern 2 -- no "by":  Two+ meaningful parts.
        #   - >=2 non-stopword parts -> attempt a non-by split (title + author)
        query_lower = query.lower().strip()
        if " by " in query_lower:
            parts = query_lower.split(" by ", 1)
            title_hint = parts[0].strip()
            author_hint = parts[1].strip().rstrip(",")
            if title_hint and author_hint:
                return True, title_hint, author_hint
            return False, None, None

        tokens = query_lower.split()
        meaningful = [t for t in tokens if t not in self._STOPWORDS and len(t) >= 2]
        if len(meaningful) < 2:
            return False, None, None

        # Treat a two-token query as a possible title/author split, too. The
        # candidate verifier rejects apparent author tokens copied from the
        # title (for example, "Harry Potter" where "Potter" is not its author).
        last_meaningful_idx = max(
            (i for i, t in enumerate(tokens) if t in meaningful),
            default=-1,
        )
        if last_meaningful_idx < 1:
            return False, None, None
        title_tokens = tokens[:last_meaningful_idx]
        # A guessed title boundary must not leave a conjunction, article, or
        # preposition at the end of the title. For example, the plain title
        # "Crime and Punishment" would otherwise become title="crime and",
        # author="punishment", triggering several expensive false discovery
        # requests. This is a boundary check only; explicit "by" queries and
        # complete title-plus-author queries retain their existing behavior.
        trailing_title_token = next(
            (token.strip(".,;:!?\"'()[]{}") for token in reversed(title_tokens)
             if token.strip(".,;:!?\"'()[]{}")),
            "",
        )
        if trailing_title_token in self._STOPWORDS or trailing_title_token == "&":
            return False, None, None

        title_hint = " ".join(title_tokens)
        author_hint = " ".join(tokens[last_meaningful_idx:])
        return True, title_hint, author_hint

    def _generate_plausible_splits(self, query: str) -> list[tuple[str, str]]:
        # Yield (title, author) pairs for a non-"by" query.
        # Iterates author_word_count from 1 to 3.  The author section must
        # start with a non-stopword word and contain at least one non-stopword
        # word of 2+ chars.
        tokens = query.strip().split()
        if not tokens:
            return []

        splits = []
        max_author_words = min(3, len(tokens) - 1)
        for author_word_count in range(1, max_author_words + 1):
            if author_word_count >= len(tokens):
                break
            title_words = tokens[:-author_word_count]
            author_words = tokens[-author_word_count:]
            stripped_author = [w.strip(".,;:!'?\"-()[]{}") for w in author_words]
            meaningful_author = [w for w in stripped_author
                                 if w.lower() not in self._STOPWORDS]
            if not meaningful_author:
                continue
            if len(meaningful_author[0]) < 2:
                continue
            title = " ".join(title_words).strip()
            if not title:
                continue
            author = " ".join(author_words).strip()
            splits.append((title, author))
        return splits

    def _score_title_hint(self, hint_norm: str, candidate_norm: str) -> float:
        # Fraction of hint tokens (excluding stopwords) found in candidate title.
        if not hint_norm:
            return 0.0
        hint_tokens = [t for t in hint_norm.split()
                       if t not in self._STOPWORDS and len(t) >= 2]
        if not hint_tokens:
            return 0.0
        cand_tokens = candidate_norm.split()
        matched = sum(1 for ht in hint_tokens if ht in cand_tokens)
        return matched / len(hint_tokens)

    def _score_author_hint(self, hint_norm: str, candidate_norm: str) -> float:
        # Token coverage: fraction of hint tokens in candidate author.
        # Single surname (1 token): full score if it appears anywhere.
        # Multi-word hint (2+ tokens): require coverage, max 0.5 for partial.
        if not hint_norm or not candidate_norm:
            return 0.0
        hint_tokens = [t for t in hint_norm.split() if t.strip()]
        if not hint_tokens:
            return 0.0
        matched = sum(1 for ht in hint_tokens if ht in candidate_norm)
        if matched == 0:
            return 0.0
        if matched / len(hint_tokens) > 0.5:
            return matched / len(hint_tokens)
        return 0.0

    def _candidate_from_search_books(
        self, books: list[dict], query: str,
        title_hint: str, author_hint: str,
    ) -> dict | None:
        """Verify a title/author split against already-fetched catalog records."""
        title_norm = self._normalize_for_matching(title_hint)
        author_norm = self._normalize_for_matching(author_hint)
        author_tokens = [
            token for token in re.findall(r"\w+", author_norm)
            if token not in self._STOPWORDS and len(token) >= 2
        ]
        for book in books or []:
            title = str(book.get("title") or "")
            author = str(book.get("author") or "")
            normalized_title = self._normalize_for_matching(title)
            normalized_author = self._normalize_for_matching(author)
            title_score = self._score_title_hint(title_norm, normalized_title)
            candidate_title_tokens = set(re.findall(r"\w+", normalized_title))
            author_is_independent = (
                " by " in query.lower()
                or not author_tokens
                or any(token not in candidate_title_tokens for token in author_tokens)
            )
            author_score = (
                self._score_author_hint(author_norm, normalized_author)
                if author_is_independent else 0.0
            )
            if author_score == 0 and author_norm and author_is_independent:
                try:
                    ascii_hint = author_norm.encode("ascii").decode("ascii")
                    if ascii_hint and any(ord(char) > 127 for char in author):
                        author_score = 1.0
                except (UnicodeDecodeError, UnicodeEncodeError):
                    pass
            if title_score >= 0.5 and author_score > 0:
                return {"title": title, "author": author, "source": book.get("source", "")}
        return None

    def _discover_candidate(
        self, query: str, title_hint: str | None, author_hint: str | None
    ) -> dict | None:
        # Query Google Books with intitle:/inauthor: operators.
        # Return first volume where both title_score and author_score >= 0.5.
        parts = []
        if title_hint:
            parts.append(f"intitle:{title_hint}")
        if author_hint:
            parts.append(f"inauthor:{author_hint}")
        gb_query = query if not parts else "+".join(parts)

        resp = get_http_session().get(
            "https://www.googleapis.com/books/v1/volumes",
            params={"q": gb_query, "maxResults": 6},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        items = resp.json().get("items", [])
        if not items:
            return None

        hint_norm = self._normalize_for_matching(title_hint) if title_hint else ""
        author_hint_norm = self._normalize_for_matching(author_hint) if author_hint else ""

        for item in items:
            vol = item.get("volumeInfo", {})
            vol_title = vol.get("title", "")
            vol_authors = vol.get("authors", [])
            vol_author = vol_authors[0] if vol_authors else ""
            title_score = self._score_title_hint(
                hint_norm, self._normalize_for_matching(vol_title))
            author_score = self._score_author_hint(
                author_hint_norm, self._normalize_for_matching(vol_author))
            # Cross-script author matching: when the hint is ASCII/Latin but the
            # candidate author contains non-ASCII characters (CJK, Cyrillic, etc.),
            # the local _score_author_hint can't connect Romanized names to native
            # script. Since the Google Books structured query (with inauthor:) already
            # matched the author name, trust the title_score as sufficient evidence.
            if author_score == 0 and author_hint_norm:
                try:
                    ascii_hint = author_hint_norm.encode("ascii").decode("ascii")
                    has_non_ascii = any(ord(c) > 127 for c in vol_author)
                    cross_script = bool(ascii_hint) and has_non_ascii
                except (UnicodeDecodeError, UnicodeEncodeError):
                    cross_script = False
                if cross_script:
                    author_score = 1.0
            if title_score >= 0.5 and author_score >= 0.5:
                return {"title": vol_title, "author": vol_author, "source": "google_books"}
        return None

    async def _try_clarification(
        self, update: Update, query: str, candidate: dict | None = None,
    ) -> bool:
        # Return True if caller should stop (clarification shown or rate-limited).
        user_id = update.effective_user.id
        norm_q = query.lower().strip()
        key = (user_id, norm_q)
        now = time.time()
        last = self._clarification_rate_limit.get(key, 0)
        is_owner = (
            self._owner_user_id is not None and user_id == self._owner_user_id
        )
        if now - last < 30 and not is_owner:
            if await self._is_owner_or_group_admin(user_id, update.effective_chat):
                is_owner = True
        if now - last < 30 and not is_owner:
            await self.app.bot.send_message(
                update.effective_chat.id,
                "⏳ <b>Please wait</b> — you're being rate-limited on this query. "
                "Try again in a few seconds.",
                parse_mode=ParseMode.HTML,
            )
            return True  # Stop caller; cooldown in effect

        needs, title_hint, author_hint = self._is_clarification_query(query)
        if not needs:
            return False  # Not a clarification query

        if candidate is None:
            discovery_started = time.perf_counter()
            candidate = await self._discover_candidate_cached(query, title_hint, author_hint)
            logger.info(
                "[perf] clarification_discovery elapsed_ms=%d matched=%s query=%r",
                round((time.perf_counter() - discovery_started) * 1000),
                candidate is not None,
                query,
            )
        if candidate is None:
            return False  # No strong match

        canonical_title = candidate["title"]
        canonical_author = candidate["author"]

        chat_id = update.effective_chat.id
        message_id = update.message.message_id
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Yes, correct!",
                                 callback_data=f"clar_yes_{user_id}"),
            InlineKeyboardButton("No, search my query",
                                 callback_data=f"clar_no_{user_id}"),
            InlineKeyboardButton("Cancel",
                                 callback_data=f"clar_cancel_{user_id}"),
        ]])
        display_title = canonical_title
        if title_hint and self._score_title_hint(
            self._normalize_for_matching(title_hint),
            self._normalize_for_matching(canonical_title),
        ) >= 1.0:
            # Preserve the user's title spelling when the verified record
            # contains every requested title term (even with a translated alias).
            display_title = (
                query.split(" by ", 1)[0].strip()
                if " by " in query.lower()
                else title_hint
            )
        prompt_text = (
            f"Did you mean {html_escape(display_title)} "
            f"by {html_escape(canonical_author)}?"
        )
        if update.effective_chat.type != "private":
            prompt_msg = await update.message.reply_text(
                prompt_text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                reply_parameters=ReplyParameters(
                    message_id=message_id,
                    allow_sending_without_reply=True,
                ),
            )
        else:
            prompt_msg = await self.app.bot.send_message(
                chat_id,
                prompt_text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        self._clarification[user_id] = {
            "query": query,
            "title_hint": title_hint,
            "author_hint": author_hint,
            "canonical_title": canonical_title,
            "canonical_author": canonical_author,
            "chat_id": chat_id,
            "message_id": prompt_msg.message_id,
            "source_message_id": message_id,
            "chat_type": update.effective_chat.type,
            "requester_id": user_id,
        }
        # Record rate-limit BEFORE showing prompt (so test can verify immediately)
        self._record_clarification_rate_limit((user_id, norm_q), now)
        return True  # Stop caller; wait for button

    def _handle_clarification_response(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, confirmed: bool,
        entry: dict | None = None,
    ) -> None:
        """Consume a validated Yes/No response and start its search."""
        user_id = update.effective_user.id
        entry = entry or self._clarification.get(user_id)
        if not entry:
            return
        self._clarification.pop(user_id, None)
        query = entry["query"]
        norm_q = query.lower().strip()
        self._record_clarification_rate_limit((user_id, norm_q), time.time())
        if confirmed:
            title_hint = entry.get("canonical_title", entry.get("title_hint", ""))
            author_hint = entry.get("canonical_author", entry.get("author_hint", ""))
            original_query = None
        else:
            # No means search the exact query the user entered. It is not a cancel.
            title_hint = author_hint = ""
            original_query = query
        asyncio.create_task(
            self._run_clarified_search(
                update, query, title_hint, author_hint,
                original_query=original_query, entry=entry, context=context,
            )
        )

    async def _run_clarified_search(
        self, update: Update, query: str, title_hint: str, author_hint: str,
        original_query: str | None = None, entry: dict | None = None,
        context: ContextTypes.DEFAULT_TYPE | None = None,
    ) -> None:
        # Perform search using the confirmed title+author hints (Yes) or
        # the original query (No).
        search_q = original_query if original_query else f"{title_hint} {author_hint}".strip()
        # Preserve the verified title/author boundary for providers. In
        # particular, don't turn a confirmed match into an unstructured AND
        # query that can lose alternate-language editions again.
        provider_query = (
            f"{title_hint} by {author_hint}"
            if original_query is None and title_hint and author_hint
            else search_q
        )
        user_id = update.effective_user.id
        entry = entry or self._clarification.get(user_id, {})
        chat_id = entry.get("chat_id", update.effective_chat.id)
        reply_to_id = (
            entry.get("source_message_id")
            if entry.get("chat_type", update.effective_chat.type) != "private"
            else None
        )
        bot = self.app.bot

        async def send_result(text: str, reply_markup=None):
            kwargs = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": ParseMode.HTML,
                "reply_markup": reply_markup,
            }
            if reply_to_id:
                kwargs["reply_to_message_id"] = reply_to_id
            try:
                return await bot.send_message(**kwargs)
            except Exception:
                # A group may have deleted the source message while the search ran.
                if "reply_to_message_id" not in kwargs:
                    raise
                kwargs.pop("reply_to_message_id", None)
                return await bot.send_message(**kwargs)

        try:
            # Run primary (title+author) and supplementary (author-only) searches
            # concurrently. The supplementary search finds other books by the same
            # author; deduplication keeps only distinct works.
            if original_query is None and title_hint and author_hint:
                safe_author = author_hint.replace('"', " ").strip()
                # Keep the plain author string for alternate/mock aggregators;
                # the production author bibliography is queried from Hardcover.
                author_query = safe_author
                logger.info("🔍 Clarification supplementary author search: %s", author_query)
                original_author_hint = str((entry or {}).get("author_hint") or "").strip()
                cross_script_author_hint = (
                    original_author_hint
                    if any(ord(char) > 127 for char in safe_author)
                    and any(char.isascii() and char.isalpha() for char in original_author_hint)
                    else ""
                )

                async def search_hardcover_author() -> list[dict]:
                    # Keep the author bibliography on Hardcover, whose search
                    # index is book/author metadata oriented. Google remains the
                    # source for the exact confirmed title and its editions.
                    if not isinstance(self.aggregator, MultiSourceBookAggregator):
                        return await self.aggregator.aggregate_book_data(
                            author_query, limit=10
                        )
                    author_queries = [safe_author]
                    if cross_script_author_hint and cross_script_author_hint.casefold() != safe_author.casefold():
                        author_queries.append(cross_script_author_hint)
                    search_sets = await asyncio.gather(*(
                        asyncio.to_thread(
                            MultiSourceBookAggregator.search_hardcover,
                            author_search_query,
                            40,
                        )
                        for author_search_query in author_queries
                    ))
                    return [book for result_set in search_sets for book in (result_set or [])]

                primary_books, extra_books = await asyncio.gather(
                    self._aggregate_search_results(provider_query, limit=10),
                    search_hardcover_author(),
                )
                # Normalise empty results to empty list.
                primary_books = primary_books or []
                extra_books = extra_books or []

                def is_confirmed_primary(book: dict) -> bool:
                    """True if this is the confirmed title by the confirmed author."""
                    t = (book.get("title") or "").lower()
                    a = (book.get("author") or "").lower()
                    return (
                        title_hint.lower() in t
                        and safe_author.lower() in a
                        and not self._is_free_sample(book.get("title", ""))
                    )

                # ── Primary: keep all books (including unknown language).
                # Language is optional in aggregator output; missing means unknown,
                # not "non-English". Free samples are always discarded.
                primary_clean = [
                    b for b in primary_books
                    if not self._is_free_sample(b.get("title", ""))
                ]
                logger.info(
                    "🔍 Supplementary author search: primary=%d primary_clean=%d extra=%d",
                    len(primary_books), len(primary_clean), len(extra_books),
                )
                # ── Supplementary: retain books in every catalog language.
                # The author match is the relevance check; language metadata
                # must not hide Chinese/Japanese or unknown-language editions.
                extra_filtered = [
                    b for b in extra_books
                    if (
                        self._author_matches_canonical(b.get("author", ""), author_hint)
                        or (cross_script_author_hint and self._author_matches_canonical(
                            b.get("author", ""), cross_script_author_hint
                        ))
                    )
                    and not self._is_free_sample(b.get("title", ""))
                ]
                standalone_books = [
                    book for book in extra_filtered
                    if not self._is_compilation_or_bundle(book.get("title", ""))
                ]
                bundle_books = [
                    book for book in extra_filtered
                    if self._is_compilation_or_bundle(book.get("title", ""))
                ]
                # Keep the provider's order within each group, but show
                # individual works before boxed collections and omnibus sets.
                extra_filtered = standalone_books + bundle_books
                # ── Merge: confirmed primary matches first, then supplementary. ────
                confirmed = [b for b in primary_clean if is_confirmed_primary(b)]
                other_primary = [b for b in primary_clean if not is_confirmed_primary(b)]
                merged = confirmed + other_primary + extra_filtered
                # For Latin-script searches, prefer English editions when a
                # matching edition exists. Keep other languages available so
                # books without an English edition remain discoverable.
                has_latin_query = any(ch.isascii() and ch.isalpha() for ch in search_q)
                has_non_latin_query = any(ch.isalpha() and not ch.isascii() for ch in search_q)
                preferred_language = (
                    "en" if has_latin_query and not has_non_latin_query else None
                )
                deduped = self._deduplicate_search_results(
                    merged, search_q, preferred_language=preferred_language
                )
                books = self._rank_search_results(
                    deduped, search_q, preferred_language=preferred_language
                )
                books.sort(key=lambda book: self._is_compilation_or_bundle(
                    book.get("title", "")
                ))
                logger.info("DEBUG RANK books=%d titles=%s", len(books),
                            [(b.get("title","")[:15], b.get("author","")[:10]) for b in books[:8]])
                logger.info("DEBUG post-rank books=%d titles=%s", len(books),
                            [b.get("title","")[:20] for b in books])
            else:
                books = await self._aggregate_search_results(provider_query, limit=10)
                books = self._rank_search_results(
                    self._deduplicate_search_results(books, search_q), search_q
                )
                if original_query is not None:
                    needs_pair_check, title_hint, author_hint = self._is_clarification_query(search_q)
                    if needs_pair_check:
                        books = self._filter_clarification_like_results(
                            books, search_q, title_hint or "", author_hint or ""
                        )
            self._set_cached_books(user_id, books)
            self._search_page_cache[user_id] = 1
            self._search_query_cache[user_id] = search_q
            if not books:
                await send_result(
                    f"No books found for '{html_escape(search_q)}'. Try a different search."
                )
                return
            # Preload Hardcover ratings for page 1 BEFORE building the UI.
            await self._preload_hardcover_ratings_for_page(books, 1, 5)
            results_text, keyboard = self._build_search_results_message(
                books, search_q, user_id, 1, 5)
            result_message = await send_result(results_text, reply_markup=keyboard)
            if result_message:
                result_message_id = result_message.message_id
                self._cache_result_message(
                    chat_id, result_message_id, user_id, books, search_q, 1
                )
                self._active_result_messages[(chat_id, user_id)] = {
                    "message_id": result_message_id,
                    "query": search_q,
                    "page": 1,
                }
        except Exception as e:
            logger.error(f"Error in _run_clarified_search: {e}", exc_info=True)

    async def _aggregate_search_results(self, query: str, limit: int = 10) -> list[dict]:
        """Reuse cached and in-flight provider searches without changing results."""
        cache_key = f"{query.lower().strip()}|{limit}"
        cache = getattr(self, "_aggregate_search_cache", {})
        ttl = getattr(self, "_AGGREGATE_SEARCH_CACHE_TTL", 120)
        now = time.time()
        entry = cache.get(cache_key)
        if entry is not None:
            cached_at, books = entry
            if now - cached_at <= ttl:
                logger.info("Search result cache hit for query=%r", query)
                return copy.deepcopy(books)
            cache.pop(cache_key, None)

        inflight = getattr(self, "_aggregate_search_inflight", None)
        if inflight is None:
            inflight = {}
            self._aggregate_search_inflight = inflight
        task = inflight.get(cache_key)
        if task is None or task.done():
            logger.info("Search result cache miss for query=%r", query)
            task = asyncio.create_task(
                self._fetch_and_cache_aggregate_search(cache_key, query, limit, cache)
            )
            inflight[cache_key] = task

            def clear_inflight(completed_task):
                if inflight.get(cache_key) is completed_task:
                    inflight.pop(cache_key, None)

            task.add_done_callback(clear_inflight)
        else:
            logger.info("Search request joined in-flight fetch for query=%r", query)

        # One cancelled Telegram update must not cancel a fetch shared by others.
        books = await asyncio.shield(task)
        return copy.deepcopy(books)

    async def _fetch_and_cache_aggregate_search(
        self, cache_key: str, query: str, limit: int, cache: dict
    ) -> list[dict]:
        started = time.perf_counter()
        books = await self.aggregator.aggregate_book_data(query, limit=limit)
        if isinstance(books, list) and books:
            max_entries = getattr(self, "_AGGREGATE_SEARCH_CACHE_MAX", 128)
            if len(cache) >= max_entries and cache_key not in cache:
                oldest_key = min(cache, key=lambda key: cache[key][0])
                cache.pop(oldest_key, None)
            cache[cache_key] = (time.time(), copy.deepcopy(books))
        logger.info(
            "[perf] aggregate_search elapsed_ms=%d results=%d query=%r",
            round((time.perf_counter() - started) * 1000),
            len(books) if isinstance(books, list) else 0,
            query,
        )
        return books

    def _record_clarification_rate_limit(self, key: tuple[int, str], timestamp: float) -> None:
        """Keep the short clarification debounce map bounded and semantically fresh."""
        cache = self._clarification_rate_limit
        for old_key, old_time in list(cache.items()):
            if timestamp - old_time >= 30:
                cache.pop(old_key, None)
        if key not in cache and len(cache) >= 2000:
            oldest_key = min(cache, key=cache.get)
            cache.pop(oldest_key, None)
        cache[key] = timestamp

    def _author_matches_canonical(self, result_author: str, canonical_author: str) -> bool:
        """Check if result author matches the canonical author (case-insensitive token overlap).

        Used to validate supplementary author-only search results.
        Rejects books by unrelated or similarly-named authors.
        """
        if not result_author or not canonical_author:
            return False
        hint_tokens = set(re.findall(r"\w+", canonical_author.casefold()))
        result_tokens = set(re.findall(r"\w+", result_author.casefold()))
        # Must share at least one non-trivial token (>= 2 chars).
        significant_hint = {t for t in hint_tokens if len(t) >= 2}
        significant_result = {t for t in result_tokens if len(t) >= 2}
        return bool(significant_hint & significant_result)

    @staticmethod
    def _is_free_sample(title: str) -> bool:
        """Return True if the title indicates a free/sample edition.

        Free samples show as separate entries from the main work and should be
        filtered out so they don't crowd distinct books.
        """
        t = title.casefold()
        return "read a free sample" in t or "free sample" in t or t.startswith("sample -")

    @staticmethod
    def _is_compilation_or_bundle(title: str) -> bool:
        normalized = re.sub(r"[^a-z0-9]+", " ", str(title or "").casefold()).strip()
        return bool(re.search(
            r"\b(?:\d+\s*book\s+(?:set|box|collection)|(?:box|boxed)\s*set|"
            r"boxset|omnibus|collection\s+\d+\s+books?)\b",
            normalized,
        ))

    @staticmethod
    def _author_hint_is_complete(author_hint: str | None, candidate_author: str | None) -> bool:
        """Check whether a supplied author is already as specific as the match."""
        hint_tokens = re.findall(r"\w+", (author_hint or "").casefold())
        candidate_tokens = re.findall(r"\w+", (candidate_author or "").casefold())
        if not hint_tokens or not candidate_tokens:
            return False
        if set(hint_tokens) == set(candidate_tokens):
            return True
        if hint_tokens[-1] != candidate_tokens[-1]:
            return False

        def given_name_initials(tokens: list[str]) -> set[str]:
            return {token[0] for token in tokens[:-1] if token}

        hint_initials = given_name_initials(hint_tokens)
        candidate_initials = given_name_initials(candidate_tokens)
        return bool(hint_initials) and hint_initials == candidate_initials

    def _discover_candidate(
        self, query: str, title_hint: str | None, author_hint: str | None,
        _log: str = "",
    ) -> dict | None:
        parts = []
        if title_hint:
            parts.append(f"intitle:{title_hint}")
        if author_hint:
            parts.append(f"inauthor:{author_hint}")
        gb_query = query if not parts else " ".join(parts)

        log_prefix = f"[discover] {_log} " if _log else "[discover] "
        logger.info(
            f"[clarification] input query={query!r} "
            f"title_hint={title_hint!r} author_hint={author_hint!r}"
        )

        def fetch_items(
            search_query: str, label: str, max_results: int,
            english_only: bool = True,
        ) -> list:
            try:
                # Do not restrict catalog language: the title may be English
                # while the edition/metadata is indexed in Chinese or Japanese.
                params = {"q": search_query, "maxResults": max_results}
                if GOOGLE_BOOKS_API_KEY:
                    params["key"] = GOOGLE_BOOKS_API_KEY
                response = get_http_session().get(
                    "https://www.googleapis.com/books/v1/volumes",
                    params=params, timeout=10,
                )
                if response.status_code != 200:
                    logger.warning(
                        f"{log_prefix}{label} request: status={response.status_code} "
                        f"totalItems=unknown query={search_query!r}"
                    )
                    return []
                data = response.json()
                items = data.get("items", []) or []
                logger.info(
                    f"{log_prefix}{label} request: status={response.status_code} "
                    f"totalItems={data.get('totalItems', 0)} items={len(items)} "
                    f"query={search_query!r}"
                )
                return items
            except Exception as exc:
                logger.warning(
                    f"{log_prefix}{label} request failed for query={search_query!r}: "
                    f"{type(exc).__name__}: {exc}"
                )
                return []

        items = fetch_items(gb_query, "structured", 6)

        hint_norm = self._normalize_for_matching(title_hint) if title_hint else ""
        author_hint_norm = self._normalize_for_matching(author_hint) if author_hint else ""

        if not hint_norm and not author_hint_norm:
            query_norm = self._normalize_for_matching(query)
            hint_norm = query_norm
            tokens = [t for t in query_norm.split()
                      if t not in self._STOPWORDS and len(t) >= 2]
            author_hint_norm = tokens[-1] if tokens else ""

        def matching_candidate(candidate_items: list, label: str) -> dict | None:
            for item in candidate_items:
                vol = item.get("volumeInfo", {})
                vol_title = vol.get("title", "")
                vol_authors = vol.get("authors", [])
                vol_author = vol_authors[0] if vol_authors else ""
                vol_title_norm = self._normalize_for_matching(vol_title)
                vol_author_norm = self._normalize_for_matching(vol_author)
                title_score = self._score_title_hint(hint_norm, vol_title_norm)
                author_hint_tokens = [
                    token for token in re.findall(r"\w+", author_hint_norm)
                    if token not in self._STOPWORDS and len(token) >= 2
                ]
                candidate_title_tokens = set(re.findall(r"\w+", vol_title_norm))
                author_is_independently_identified = (
                    " by " in query.lower()
                    or not author_hint_tokens
                    or any(token not in candidate_title_tokens for token in author_hint_tokens)
                )
                author_score = (
                    self._score_author_hint(author_hint_norm, vol_author_norm)
                    if author_hint_norm and author_is_independently_identified else 0.0
                )
                # Cross-script author matching: when the query author is ASCII but the
                # result author contains non-ASCII characters (CJK, Cyrillic, etc.),
                # the token-based scoring above returns 0 because tokens don't overlap.
                # Boost the score so cross-script candidates with good title matches
                # can still be discovered (e.g. "sugaru miaki" → "三秋縋").
                if author_score == 0 and author_hint_norm:
                    try:
                        ascii_hint = author_hint_norm.encode("ascii").decode("ascii")
                        has_non_ascii = any(ord(c) > 127 for c in vol_author)
                        if ascii_hint and has_non_ascii:
                            author_score = 1.0
                    except (UnicodeDecodeError, UnicodeEncodeError):
                        pass
                passed = title_score >= 0.5 and author_score > 0
                logger.info(
                    f"{log_prefix}{label} candidate title={vol_title!r} author={vol_author!r} "
                    f"title_score={title_score:.2f} author_score={author_score:.2f} "
                    f"pass={passed}"
                )
                if passed:
                    return {"title": vol_title, "author": vol_author, "source": "google_books"}
            return None

        cand = matching_candidate(items, "structured")
        if cand:
            return cand

        # Retry the combined fields without a language filter before broadening
        # to author-only/title-only queries. This preserves both clues while
        # allowing catalog records stored in their publication language.
        if title_hint and author_hint:
            safe_title = title_hint.replace('"', " ").strip()
            safe_author = author_hint.replace('"', " ").strip()
            all_language_query = (
                f'intitle:"{safe_title}" inauthor:"{safe_author}"'
            )
            all_language_items = fetch_items(
                all_language_query, "structured_all_languages", 40,
                english_only=False,
            )
            if all_language_items:
                cand = matching_candidate(
                    all_language_items, "structured_all_languages"
                )
                if cand:
                    return cand

            # Google Books occasionally fails to honor fielded title/author
            # operators for translated editions. Try the same evidence as a
            # plain combined query; still accept only candidates that pass the
            # strict title AND author matcher above.
            combined_query = f'"{safe_title}" "{safe_author}"'
            combined_items = fetch_items(
                combined_query, "combined_fallback", 40, english_only=False
            )
            if combined_items:
                cand = matching_candidate(combined_items, "combined_fallback")
                if cand:
                    return cand

            # The quoted/title-author searches can miss common short author
            # names (for example, `Origin Dan`) even though Google's ordinary
            # combined search returns the exact book. Keep the same strict
            # title-and-author validation so broad-query noise cannot trigger
            # a false clarification.
            plain_items = fetch_items(
                query, "plain_combined_fallback", 40, english_only=False
            )
            if plain_items:
                cand = matching_candidate(plain_items, "plain_combined_fallback")
                if cand:
                    return cand

        # If the strict combined lookup misses, search by author alone first.
        # Google Books can rank an incomplete title+author query poorly even
        # when it has the right volume; candidate validation still requires both.
        if title_hint and author_hint:
            safe_author = author_hint.replace('"', " ").strip()
            author_query = f'inauthor:"{safe_author}"'
            author_items = fetch_items(
                author_query, "author_fallback", 40, english_only=False
            )
            if author_items:
                cand = matching_candidate(author_items, "author_fallback")
                if cand:
                    return cand

            safe_title = title_hint.replace('"', " ").strip()
            fallback_query = f'intitle:"{safe_title}"'
            # Search by title across languages as the final discovery fallback.
            fb_items = fetch_items(
                fallback_query, "fallback", 40, english_only=False
            )
            if fb_items:
                cand = matching_candidate(fb_items, "fallback")
                if cand:
                    return cand
        return None

    def _get_cached_books(self, user_id: int) -> list | None:
        """Return cached search results for *user_id*, or None if missing/expired."""
        entry = self.search_cache.get(user_id)
        if entry is None:
            return None
        ts = entry[1]
        if time.time() - ts > self._SEARCH_CACHE_TTL:
            self.search_cache.pop(user_id, None)
            return None
        return entry[0]

    def _cache_result_message(
        self, chat_id: int, message_id: int, user_id: int,
        books: list, query_text: str, page_num: int = 1,
    ) -> None:
        """Bind a result message's buttons to that search's own result list."""
        cache = getattr(self, "_result_message_cache", None)
        if cache is None:
            cache = self._result_message_cache = {}
        now = time.time()
        ttl = getattr(self, "_RESULT_MESSAGE_CACHE_TTL", 60 * 60)
        for key, state in list(cache.items()):
            if now - state.get("timestamp", 0) > ttl:
                cache.pop(key, None)
        max_entries = getattr(self, "_RESULT_MESSAGE_CACHE_MAX", 1000)
        if len(cache) >= max_entries:
            oldest = min(cache, key=lambda key: cache[key].get("timestamp", 0))
            cache.pop(oldest, None)
        cache[(chat_id, message_id)] = {
            "user_id": user_id,
            "books": books,
            "query": query_text,
            "page": page_num,
            "timestamp": now,
        }

    def _get_result_message_state(
        self, chat_id: int, message_id: int, user_id: int,
    ) -> dict | None:
        cache = getattr(self, "_result_message_cache", {})
        key = (chat_id, message_id)
        state = cache.get(key)
        if state is None:
            return None
        ttl = getattr(self, "_RESULT_MESSAGE_CACHE_TTL", 60 * 60)
        if time.time() - state.get("timestamp", 0) > ttl:
            cache.pop(key, None)
            return None
        if state.get("user_id") != user_id:
            return None
        return state

    def _set_cached_books(self, user_id: int, books: list) -> None:
        """Store search results for *user_id* with a timestamp."""
        # Bound cache size — drop oldest entries when full
        if len(self.search_cache) >= self._SEARCH_CACHE_MAX:
            # Evict the 10% oldest by timestamp
            sorted_users = sorted(
                self.search_cache, key=lambda k: self.search_cache[k][1]
            )
            for uid in sorted_users[: max(1, len(sorted_users) // 10)]:
                self.search_cache.pop(uid, None)
        self.search_cache[user_id] = (books, time.time(), "")

    def _get_inline_callback_data(self, callback_key: str) -> dict | None:
        """Return cached book data for *callback_key*, or None if missing/expired."""
        entry = self._inline_callback_cache.get(callback_key)
        if entry is None:
            return None
        data, ts = entry
        if time.time() - ts > self._INLINE_CALLBACK_CACHE_TTL:
            self._inline_callback_cache.pop(callback_key, None)
            return None
        return data

    def _set_inline_callback_data(self, callback_key: str, book_data: dict) -> None:
        """Store book data for *callback_key* with a timestamp."""
        # Bound cache size — drop oldest entries when full
        if len(self._inline_callback_cache) >= 1000:  # Reasonable limit for inline callbacks
            # Evict the 10% oldest by timestamp
            sorted_keys = sorted(
                self._inline_callback_cache, key=lambda k: self._inline_callback_cache[k][1]
            )
            for key in sorted_keys[: max(1, len(sorted_keys) // 10)]:
                self._inline_callback_cache.pop(key, None)
        self._inline_callback_cache[callback_key] = (book_data, time.time())

    # ── Handler registration ──────────────────────────────────────────────────

    def setup_handlers(self):
        """Register all command and callback handlers."""
        self.app.add_error_handler(self._handle_application_error)
        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CommandHandler("help", self.help_command))
        self.app.add_handler(CommandHandler("portal", self.portal_command))
        self.app.add_handler(CommandHandler("recom", self.recom_command))
        self.app.add_handler(CommandHandler("bookshelf", self.bookshelf_command))
        self.app.add_handler(CommandHandler("favorites", self.favorites_command))
        self.app.add_handler(CommandHandler("authorize", self.authorize_command))
        self.app.add_handler(CommandHandler("unauthorize", self.unauthorize_command))
        self.app.add_handler(CommandHandler("admins", self.admins_command))
        self.app.add_handler(CommandHandler("connect", self.connect_command))
        self.app.add_handler(CommandHandler("connections", self.connections_command))
        self.app.add_handler(CommandHandler("disconnect", self.disconnect_command))
        self.app.add_handler(CommandHandler("id", self.id_command))
        self.app.add_handler(CommandHandler("index", self.index_command))
        self.app.add_handler(CommandHandler("cancel", self._channel_manager.cancel_command))
        self.app.add_handler(CommandHandler("channelmanager", self._channel_manager.open_command))
        # Private Channel Manager commands; intentionally omitted from Telegram command suggestions.
        self.app.add_handler(CommandHandler("clone", self._channel_manager.clone_command))
        self.app.add_handler(CommandHandler("autoforward", self._channel_manager.autoforward_command))
        # These work as private-chat commands but are intentionally omitted
        # from Telegram's public command suggestions.
        self.app.add_handler(CommandHandler("promote", self._channel_manager.promote_command))
        self.app.add_handler(CommandHandler("fullpromote", self._channel_manager.fullpromote_command))
        self.app.add_handler(CommandHandler("demote", self._channel_manager.demote_command))
        self.app.add_handler(CommandHandler("channelapprove", self._channel_manager.approve_channel_command))
        self.app.add_handler(CommandHandler("channelrevoke", self._channel_manager.revoke_channel_command))
        self.app.add_handler(CommandHandler("channelapprovals", self._channel_manager.list_approved_channels_command))
        self.app.add_handler(CommandHandler("search", self.search_command))
        self.app.add_handler(CommandHandler("ping", self.ping_command))
        self.app.add_handler(CallbackQueryHandler(
            self._channel_manager.handle_callback, pattern=r"^cm:"
        ))
        self.app.add_handler(CallbackQueryHandler(self.button_callback))
        self.app.add_handler(InlineQueryHandler(self.inline_search))
        self.app.add_handler(MessageHandler(filters.FORWARDED, self._index_forwarded_message))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._index_text_input))
        self.app.add_handler(MessageHandler(
            filters.ALL & ~filters.COMMAND,
            self._channel_manager.handle_message,
            block=True,
        ), group=1)

    async def _handle_application_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        error = context.error
        if error is None:
            return
        logger.error(
            "[telegram] unhandled update error update_type=%s error_type=%s",
            type(update).__name__, type(error).__name__,
            exc_info=(type(error), error, error.__traceback__),
        )

    async def _post_init(self, application: Application) -> None:
        await self._configure_telegram_commands(application)
        self._channel_manager_task = asyncio.create_task(
            self._channel_manager.run_scheduler(application.bot),
            name="channel-manager-scheduler",
        )
        if self.webhook_mode:
            return
        repository = self._get_channel_index_repository()
        if repository is None:
            return
        try:
            enabled = await asyncio.to_thread(repository.list_enabled)
            if not enabled:
                logger.info("[channel-index] no active indexes; MTProto listener not started")
                return
            await self._start_channel_index_listener(application.bot)
        except Exception as exc:
            runtime = getattr(self, "_channel_index_runtime", None)
            if runtime is not None:
                try:
                    await runtime.close()
                except Exception:
                    pass
                self._channel_index_runtime = None
            logger.warning(
                "[channel-index] listener unavailable; bot polling continues error=%s",
                type(exc).__name__,
            )

    async def _start_channel_index_listener(self, bot: Any | None = None) -> bool:
        if self.webhook_mode:
            return False
        if self._channel_index_runtime is not None:
            return True
        api_id = os.getenv("API_ID", "").strip()
        api_hash = os.getenv("API_HASH", "").strip()
        repository = self._get_channel_index_repository()
        if repository is None or not api_id or not api_hash:
            return False
        runtime = ChannelIndexRuntime(
            bot or self.app.bot, repository, int(api_id), api_hash, self.token
        )
        try:
            await runtime.start()
        except Exception:
            try:
                await runtime.close()
            except Exception:
                pass
            raise
        self._channel_index_runtime = runtime
        logger.info("[channel-index] MTProto update listener started")
        return True

    async def _ensure_channel_index_listener(self) -> bool:
        try:
            return await self._start_channel_index_listener()
        except Exception as exc:
            logger.warning(
                "[channel-index] could not start listener; bot polling continues error=%s",
                type(exc).__name__,
            )
            return False

    async def _resolve_channel_admin_username(
        self, username: str,
    ) -> tuple[int | None, str | None]:
        """Resolve a member username through the shared MTProto listener."""
        started_for_lookup = self._channel_index_runtime is None
        if started_for_lookup and not await self._ensure_channel_index_listener():
            return None, None
        runtime = self._channel_index_runtime
        if runtime is None:
            return None, None
        try:
            return await runtime.resolve_sender_username(username)
        finally:
            if started_for_lookup:
                await self._stop_channel_index_listener_if_idle()

    async def _delete_channel_messages_mtproto(
        self, channel_id: int, message_ids: list[int],
    ) -> bool:
        """Delete saved channel posts through the shared bot-authenticated MTProto client."""
        started_for_delete = self._channel_index_runtime is None
        if started_for_delete and not await self._ensure_channel_index_listener():
            return False
        runtime = self._channel_index_runtime
        if runtime is None or runtime.client is None:
            return False
        try:
            await runtime.client.delete_messages(int(channel_id), [int(value) for value in message_ids])
            return True
        except Exception as exc:
            logger.warning(
                "[channel-manager] MTProto delete unavailable channel_id=%s messages=%s error=%s",
                channel_id, len(message_ids), type(exc).__name__,
            )
            return False
        finally:
            if started_for_delete:
                await self._stop_channel_index_listener_if_idle()

    async def _stop_channel_index_listener_if_idle(self) -> None:
        runtime = self._channel_index_runtime
        repository = self._get_channel_index_repository()
        if runtime is None or repository is None:
            return
        try:
            enabled = await asyncio.to_thread(repository.list_enabled)
        except Exception as exc:
            logger.warning("[channel-index] could not check active indexes error=%s", type(exc).__name__)
            return
        if not enabled:
            await runtime.close()
            self._channel_index_runtime = None
            logger.info("[channel-index] no active indexes; MTProto listener stopped")

    async def _post_shutdown(self, application: Application) -> None:
        manager_task = getattr(self, "_channel_manager_task", None)
        if manager_task is not None:
            manager_task.cancel()
            try:
                await manager_task
            except asyncio.CancelledError:
                pass
            self._channel_manager_task = None
        await self._channel_manager.close()
        runtime = getattr(self, "_channel_index_runtime", None)
        if runtime is not None:
            await runtime.close()
            self._channel_index_runtime = None

    def _get_channel_index_repository(self) -> MongoChannelIndexRepository | None:
        uri = os.getenv("MONGODB_URI", "").strip()
        if not uri:
            return None
        if getattr(self, "_channel_index_repository", None) is None:
            database_name = os.getenv("MONGODB_DB_NAME", "annie_db").strip() or "annie_db"
            self._channel_index_repository = MongoChannelIndexRepository(uri, database_name)
        return self._channel_index_repository

    async def _configure_telegram_commands(self, application: Application | None = None):
        """Publish command suggestions through Telegram so BotFather setup is unnecessary."""
        bot = (application or self.app).bot
        await self._load_bot_admin_ids(force=True)
        commands = [
            BotCommand("start", "Welcome to Annie Search"),
            BotCommand("help", "How to use Annie"),
            BotCommand("portal", "Open Annie Search Portal"),
            BotCommand("recom", "Open Annie Recommendations"),
            BotCommand("bookshelf", "Open your Bookshelf"),
            BotCommand("favorites", "Open your Favourites"),
            BotCommand("search", "Search books by title or author"),
            BotCommand("ping", "Check bot status and uptime"),
        ]
        commands.sort(key=lambda command: command.command)
        channel_commands = [
            BotCommand("connect", "Connect a channel by ID or username"),
            BotCommand("connections", "List connected channels"),
            BotCommand("disconnect", "Choose a channel to disconnect"),
            BotCommand("index", "Manage channel indexes"),
            BotCommand("channelmanager", "Open Channel Manager"),
            BotCommand("cancel", "Cancel the current Channel Manager step"),
        ]
        channel_commands.sort(key=lambda command: command.command)
        for scope, scoped_commands in (
            (None, commands),
            (BotCommandScopeAllPrivateChats(), commands),
            (BotCommandScopeAllGroupChats(), commands),
        ):
            try:
                if scope is None:
                    await bot.set_my_commands(scoped_commands)
                else:
                    await bot.set_my_commands(scoped_commands, scope=scope)
            except Exception as exc:
                # Command menu setup is helpful but should never prevent startup.
                logger.warning("Could not publish Telegram command menu: %s", type(exc).__name__)
        owner_id = getattr(self, "_owner_user_id", None)
        if owner_id is not None:
            owner_commands = commands + channel_commands + [
                BotCommand("authorize", "Authorize a user ID"),
                BotCommand("unauthorize", "Revoke an authorized user"),
                BotCommand("admins", "List authorized users"),
                BotCommand("channelapprove", "Approve a Channel Manager channel"),
                BotCommand("channelrevoke", "Revoke Channel Manager access"),
                BotCommand("channelapprovals", "List approved channels"),
            ]
            owner_commands.sort(key=lambda command: command.command)
            try:
                await bot.set_my_commands(
                    owner_commands, scope=BotCommandScopeChat(chat_id=owner_id)
                )
            except Exception as exc:
                logger.warning("Could not publish owner command menu: %s", type(exc).__name__)

    async def _refresh_user_command_scope(
        self, bot, user_id: int, *, force: bool = False,
    ) -> None:
        """Show Channel Manager commands only to the owner or connected channel admins."""
        if self._owner_user_id is not None and int(user_id) == self._owner_user_id:
            return  # The owner-specific command scope is installed at startup.
        user_id = int(user_id)
        lock = self._user_command_scope_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            cached = self._user_command_scope_cache.get(user_id)
            if not force and cached and cached[0] > now:
                return
            try:
                has_access = await self._channel_manager.has_connected_channel_access(user_id, bot)
                if cached and cached[1] == has_access:
                    self._user_command_scope_cache[user_id] = (now + 15, has_access)
                    return
                scope = BotCommandScopeChat(chat_id=user_id)
                if has_access:
                    public_commands = [
                        BotCommand("start", "Welcome to Annie Search"),
                        BotCommand("help", "How to use Annie"),
                        BotCommand("portal", "Open Annie Search Portal"),
                        BotCommand("recom", "Open Annie Recommendations"),
                        BotCommand("bookshelf", "Open your Bookshelf"),
                        BotCommand("favorites", "Open your Favourites"),
                        BotCommand("search", "Search books by title or author"),
                        BotCommand("ping", "Check bot status and uptime"),
                    ]
                    channel_commands = [
                        BotCommand("connect", "Connect a channel by ID or username"),
                        BotCommand("connections", "List connected channels"),
                        BotCommand("disconnect", "Choose a channel to disconnect"),
                        BotCommand("index", "Manage channel indexes"),
                        BotCommand("channelmanager", "Open Channel Manager"),
                        BotCommand("cancel", "Cancel the current Channel Manager step"),
                    ]
                    await bot.set_my_commands(
                        sorted(public_commands + channel_commands, key=lambda command: command.command),
                        scope=scope,
                    )
                else:
                    await bot.delete_my_commands(scope=scope)
                self._user_command_scope_cache[user_id] = (time.monotonic() + 15, has_access)
            except Exception as exc:
                logger.warning("Could not refresh private command menu user_id=%s error=%s", user_id, type(exc).__name__)

    def _schedule_user_command_scope_refresh(
        self, bot, user_id: int, *, force: bool = False,
    ) -> None:
        task = asyncio.create_task(
            self._refresh_user_command_scope(bot, user_id, force=force)
        )
        self._user_command_scope_tasks.add(task)
        task.add_done_callback(self._user_command_scope_tasks.discard)

    def process_update(self, raw_update: dict) -> bool:
        """Process a single update dict received from Telegram webhook.
        Returns True if processed, False otherwise.
        """
        try:
            update = Update.de_json(raw_update, self.app.bot)
            message = update.effective_message
            if (
                message is not None
                and getattr(message, "text", None)
                and message.text.lstrip().startswith("/")
                and getattr(message, "date", None) is not None
                and message.date.timestamp() < int(self._started_at)
            ):
                logger.info(
                    "Ignoring pre-startup command update_id=%s message_date=%s",
                    getattr(update, "update_id", "unknown"),
                    message.date.isoformat(),
                )
                return True
            loop = None
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            coro = self.app.process_update(update)
            future = asyncio.ensure_future(coro)
            loop.run_until_complete(future)

            result = future.result()
            if isinstance(result, Exception):
                logger.error(f"Handler raised: {result}")
                return False
            return True

        except Exception as e:
            logger.error(f"Error processing update: {e}", exc_info=True)
            return False

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Central error handler for transient network errors."""
        err = context.error
        if isinstance(err, (NetworkError, TimedOut)):
            logger.warning(f"🌐 Transient network error (auto-retrying): {err!r}")
            return
        logger.error("Unhandled exception while processing update:", exc_info=err)

    # ── Commands ────────────────────────────────────────────────────────────────

    @staticmethod
    def _mini_app_url(page: str = "", inline_ticket: str = "") -> str | None:
        """Return the configured Mini App URL, optionally targeting a page/launch."""
        configured = os.getenv("ANNIE_APP_URL", "").strip()
        if not configured:
            return None
        try:
            parsed = urlsplit(configured)
        except ValueError:
            return None
        if parsed.scheme != "https" or not parsed.netloc:
            return None
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if page:
            query["page"] = page
        else:
            query.pop("page", None)
        if inline_ticket:
            query["inline_ticket"] = inline_ticket
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", urlencode(query), parsed.fragment))

    def _issue_inline_app_ticket(self, telegram_user) -> str:
        """Issue a short-lived signed launch token usable across Koyeb replicas."""
        return issue_inline_token({
            "id": telegram_user.id,
            "first_name": getattr(telegram_user, "first_name", ""),
            "language_code": getattr(telegram_user, "language_code", ""),
        }, self.token, purpose="ticket", ttl_seconds=900)

    def _mini_app_markup(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        page: str = "", label: str = "Annie Search Portal",
    ) -> InlineKeyboardMarkup | None:
        url = self._mini_app_url(page)
        if not url:
            return None
        username = (context.bot.username or "").lstrip("@")
        chat_type = getattr(getattr(update, "effective_chat", None), "type", "private")
        # Telegram permits Web App buttons in private chats and supplies signed
        # initData directly to the configured HTTPS URL. Group chats require a
        # bot deep link, which launches the bot's configured Main Mini App.
        if chat_type == "private":
            return InlineKeyboardMarkup([[
                InlineKeyboardButton(label, web_app=WebAppInfo(url=url))
            ]])
        if not username:
            return None
        start_parameter = {
            "recommendations": "recom",
            "bookshelf": "bookshelf",
            "favorites": "favorites",
        }.get(page, "portal")
        # Reusable Telegram Main Mini App link for group chats. It creates fresh
        # signed launch data and avoids process-local one-use tickets.
        button = InlineKeyboardButton(
            label,
            url=f"https://t.me/{username}?startapp={start_parameter}",
        )
        return InlineKeyboardMarkup([[button]])

    def _start_keyboard(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        show_channel_manager: bool = False,
    ) -> InlineKeyboardMarkup:
        """Build the compact welcome menu, retaining a fallback if unconfigured."""
        portal = self._mini_app_markup(update, context, label="🔎 Annie Search Portal")
        recommendations = self._mini_app_markup(
            update, context, "recommendations", "✨ Annie Recommendations"
        )
        bookshelf = self._mini_app_markup(update, context, "bookshelf", "📚 My Bookshelf")
        favorites = self._mini_app_markup(update, context, "favorites", "♥ Favourites")
        portal_button = (
            portal.inline_keyboard[0][0] if portal else
            InlineKeyboardButton("🔎 Annie Search Portal", callback_data="start_portal")
        )
        recommendations_button = (
            recommendations.inline_keyboard[0][0] if recommendations else
            InlineKeyboardButton(
                "✨ Annie Recommendations", callback_data="start_recommendations"
            )
        )
        bookshelf_button = (
            bookshelf.inline_keyboard[0][0] if bookshelf else
            InlineKeyboardButton("📚 My Bookshelf", callback_data="start_bookshelf")
        )
        favorites_button = (
            favorites.inline_keyboard[0][0] if favorites else
            InlineKeyboardButton("♥ Favourites", callback_data="start_favorites")
        )
        rows = [
            [InlineKeyboardButton("❔ Help", callback_data="start_help"),
             InlineKeyboardButton("✦ Features", callback_data="start_features")],
            [portal_button],
            [recommendations_button],
            [bookshelf_button, favorites_button],
        ]
        if show_channel_manager:
            rows.append([InlineKeyboardButton(
                "📢 Channel Manager", callback_data="cm:home"
            )])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def _start_text() -> str:
        return (
            "✨ <b>Welcome to Annie Search</b>\n"
            "<i>Your next great read starts here.</i>\n\n"
            "Search books, explore their details, and find recommendations "
            "shaped around what you love to read. Save books to your Bookshelf "
            "or keep favourites close at hand.\n\n"
            "<b>Opening the Mini App inline?</b>\n"
            "Type <code>@AnnieBooks_bot .portal</code> or "
            "<code>@AnnieBooks_bot .recom</code>, then tap Annie’s launch button."
        )

    @staticmethod
    def _back_to_start_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("← Back to start", callback_data="start_back")
        ]])

    @staticmethod
    def _help_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("← Back to start", callback_data="start_back")],
        ])

    @staticmethod
    def _misc_keyboard() -> InlineKeyboardMarkup:
        # Keep older Index/connection flows pointed at the current channel menu.
        return InlineKeyboardMarkup([[InlineKeyboardButton(
            "← Channel Manager", callback_data="cm:home"
        )]])

    @staticmethod
    def _help_text() -> str:
        return """<b>✦ Using Annie</b>

<b>Search in a chat</b>
Send <code>/search title or author</code> to search for a book.
Tap a result to open its details. Use the buttons to browse results or download a cover.

<b>Search inline</b>
Type <code>@AnnieBooks_bot title or author</code> in any chat, then choose a result to share.
To open the Mini App inline, type <code>@AnnieBooks_bot .portal</code> or <code>@AnnieBooks_bot .recom</code>, then tap the launch button above the results.

<b>Annie Search Portal</b>
Open the portal to search books, explore trending and genre shelves, and browse similar titles with <i>More Like This</i>.

<b>Annie Recommendations</b>
Add at least one book or author you’ve read or liked, choose genres, or pick moods. Annie will use those clues to suggest books.

<b>My Bookshelf</b>
Save books to My Books, move the ones you love to Favourites with the Like button, and search, sort, or switch between grid and list views. Your lists sync to your Telegram account when cloud sync is configured.

<b>Commands</b>
<code>/portal</code> · Open the portal
<code>/recom</code> · Open recommendations
<code>/bookshelf</code> · Open My Bookshelf
<code>/favorites</code> · Open Favourites
<code>/help</code> · Show this guide
<code>/ping</code> · Check bot status

"""

    @staticmethod
    def _connections_help_text() -> str:
        return """<b>🔗 Channel connections</b>

Use these commands in a private chat with Annie. Add Annie as an administrator in the channel first. Only that channel’s owner or an administrator can connect or disconnect it.

<code>/connect &lt;channel_id or @username&gt;</code> · Connect a channel
<code>/connections</code> · List connected channel names
<code>/disconnect</code> · Choose a channel to disconnect

Use a channel ID (usually starts with <code>-100</code>) or a public channel’s <code>@username</code>."""

    async def _has_channel_tools_access(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> bool:
        if self._is_bot_owner(update):
            return True
        if not update.effective_user:
            return False
        return await self._channel_manager.has_connected_channel_access(
            update.effective_user.id, context.bot
        )

    async def index_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None or update.effective_chat.type != "private":
            await update.effective_message.reply_text("Use /index in a private chat with Annie.")
            return
        if len(context.args) > 1:
            await update.effective_message.reply_text(
                "Usage: /index [on|off|yes|no|1m|5m|10m]"
            )
            return
        action, value = "open", ""
        if context.args:
            option = context.args[0].casefold()
            if option in {"on", "yes", "off", "no"}:
                action, value = "toggle", "1" if option in {"on", "yes"} else "0"
            elif option in {"1m", "5m", "10m"}:
                action, value = "delay", option[:-1]
            else:
                await update.effective_message.reply_text(
                    "Usage: /index [on|off|yes|no|1m|5m|10m]"
                )
                return
        await self._show_index_channel_picker(update, context, action, value)

    async def _show_index_channel_picker(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        action: str = "open", value: str = "",
    ) -> None:
        repository = self._get_channel_connection_repository()
        if repository is None:
            text, markup = "Channel connections need cloud storage. Configure MONGODB_URI first.", self._misc_keyboard()
            await self._send_or_edit(update, text, markup)
            return
        try:
            channels = await asyncio.to_thread(repository.list_channels)
        except Exception as exc:
            logger.warning("[channel-index] could not list channels error=%s", type(exc).__name__)
            await self._send_or_edit(update, "I couldn’t load connected channels. Please try again.", self._misc_keyboard())
            return
        eligible = await self._connected_channels_administered_by(
            context.bot, update.effective_user.id, channels
        )
        if not eligible:
            await self._send_or_edit(
                update,
                "You don’t have a connected channel to manage yet. Connect one with <code>/connect &lt;channel_id or @username&gt;</code>.",
                self._misc_keyboard(),
            )
            return
        if len(eligible) == 1:
            await self._select_index_channel(
                update, context, int(eligible[0]["id"]), action, value
            )
            return
        rows = [[InlineKeyboardButton(
            str(channel["name"])[:60],
            callback_data=f"index_choose:{int(channel['id'])}:{action}:{value or '-'}",
        )] for channel in eligible]
        rows.append([InlineKeyboardButton("← Channel Manager", callback_data="cm:home")])
        prompt = "Choose one channel to manage at a time:"
        if action == "toggle":
            prompt = f"Turn automatic index updates {'on' if value == '1' else 'off'} for which channel?"
        elif action == "delay":
            prompt = f"Set the index update delay to {value} minute(s) for which channel?"
        await self._send_or_edit(update, prompt, InlineKeyboardMarkup(rows))

    async def _select_index_channel(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        channel_id: int, action: str = "open", value: str = "",
    ) -> None:
        raw_channel = str(int(channel_id))
        channel = await self._private_channel_target(update, context, raw_channel)
        if channel is None:
            return
        if action == "open":
            await self._show_index_settings(update, context, channel_id)
            return

        repository = self._get_channel_index_repository()
        if repository is None:
            await self._send_or_edit(update, "Channel indexes need MongoDB storage.", self._misc_keyboard())
            return
        current = await asyncio.to_thread(repository.get, channel_id) or {}
        if action == "toggle":
            desired = value == "1"
            if desired and not current.get("targets"):
                await self._send_or_edit(
                    update,
                    "Register at least one existing index post before turning updates on.",
                    self._index_settings_keyboard(channel_id, current),
                )
                return
            if desired and not await self._ensure_channel_index_listener():
                await self._send_or_edit(
                    update,
                    "Automatic index updates need API_ID and API_HASH, and must run in polling mode. Annie could not start the listener, so updates remain off.",
                    self._index_settings_keyboard(channel_id, current),
                )
                return
            await asyncio.to_thread(repository.update, channel_id, {"enabled": desired})
            if not desired and self._channel_index_runtime is not None:
                await self._channel_index_runtime.cancel_channel(channel_id)
                await self._stop_channel_index_listener_if_idle()
            await self._send_or_edit(
                update,
                f"Automatic index updates are <b>{'on' if desired else 'off'}</b> for <b>{html_escape(channel.title or 'this channel')}</b>.",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("Open index settings", callback_data=f"index_choose:{channel_id}:open:-")],
                    [InlineKeyboardButton("← Channel Manager", callback_data="cm:home")],
                ]),
            )
            return
        elif action == "delay":
            await asyncio.to_thread(repository.update, channel_id, {"delay_minutes": int(value)})
            delay_unit = "minute" if int(value) == 1 else "minutes"
            await self._send_or_edit(
                update,
                f"Delay set to <b>{int(value)} {delay_unit}</b> for <b>{html_escape(channel.title or 'this channel')}</b>. Annie will update the index {int(value)} {delay_unit} after a new post.",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("Open index settings", callback_data=f"index_choose:{channel_id}:open:-")],
                    [InlineKeyboardButton("← Channel Manager", callback_data="cm:home")],
                ]),
            )
            return
        await self._show_index_settings(update, context, channel_id)

    @staticmethod
    async def _send_or_edit(
        update: Update, text: str, markup: InlineKeyboardMarkup | None = None
    ) -> None:
        query = update.callback_query
        if query is not None:
            try:
                await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            except BadRequest as exc:
                if "message is not modified" not in str(exc).casefold():
                    raise
        else:
            await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)

    @staticmethod
    def _index_settings_keyboard(channel_id: int, config: dict | None) -> InlineKeyboardMarkup:
        config = config or {}
        enabled = bool(config.get("enabled"))
        delay = int(config.get("delay_minutes") or 5)
        rows = [[InlineKeyboardButton(
            "Turn updates off" if enabled else "Turn updates on",
            callback_data=f"index_toggle:{channel_id}:{0 if enabled else 1}",
        )]]
        rows.append([
            InlineKeyboardButton(
                f"{'✓ ' if delay == minutes else ''}{minutes} min",
                callback_data=f"index_delay:{channel_id}:{minutes}",
            ) for minutes in (1, 5, 10)
        ])
        rows.append([
            InlineKeyboardButton("Index Format", callback_data=f"index_format:{channel_id}"),
            InlineKeyboardButton("Index lists", callback_data=f"index_lists:{channel_id}"),
        ])
        rows.append([InlineKeyboardButton(
            f"Excluded senders ({len(config.get('excluded_sender_ids') or [])})",
            callback_data=f"index_exclusions:{channel_id}",
        )])
        rows.extend([
            [InlineKeyboardButton("← Choose channel", callback_data="misc_index"),
             InlineKeyboardButton("← Channel Manager", callback_data=f"cm:channel:{channel_id}")],
        ])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def _index_format_keyboard(channel_id: int, config: dict | None) -> InlineKeyboardMarkup:
        config = config or {}
        mode = str(config.get("entry_mode") or ("prefix" if config.get("title_mode") == "prefix" else "text"))
        prefix = str(config.get("entry_prefix") or config.get("title_prefix") or "")
        bullet = str(config.get("entry_bullet") or "🔹")
        sort_order = str(config.get("sort_order") or "added")
        source_options = [
            ("Text line", "text"), ("One #hashtag", "hashtags"),
            ("🔗 First link", "links"), ("🖼 Image caption", "image"),
            ("📎 File name", "file"), ("Custom prefix", "prefix"),
        ]
        rows = [
            [InlineKeyboardButton(
                f"{'✓ ' if mode == option else ''}{label}",
                callback_data=f"index_entry_source:{channel_id}:{option}",
            ) for label, option in source_options[offset:offset + 2]]
            for offset in range(0, len(source_options), 2)
        ]
        rows.append([
            InlineKeyboardButton(
                f"{'✓ ' if sort_order == 'alphabetical' else ''}A–Z",
                callback_data=f"index_sort:{channel_id}:alphabetical",
            ),
            InlineKeyboardButton(
                f"{'✓ ' if sort_order == 'added' else ''}Order added",
                callback_data=f"index_sort:{channel_id}:added",
            ),
        ])
        if mode == "prefix":
            rows.append([InlineKeyboardButton(
                f"Set prefix{': ' + prefix if prefix else ''}",
                callback_data=f"index_prefix:{channel_id}",
            )])
        rows.append([InlineKeyboardButton(
            f"Set default bullet: {bullet}", callback_data=f"index_bullet:{channel_id}"
        )])
        rows.append([InlineKeyboardButton("← Index settings", callback_data=f"index_choose:{channel_id}:open:-")])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def _index_lists_keyboard(channel_id: int, config: dict | None) -> InlineKeyboardMarkup:
        targets = list((config or {}).get("targets") or [])
        rows = []
        if targets:
            rows.extend([
                [InlineKeyboardButton("Set list markers", callback_data=f"index_categories:{channel_id}"),
                 InlineKeyboardButton("Set placeholder bullet", callback_data=f"index_bullet_target:{channel_id}")],
                [InlineKeyboardButton("Import older posts", callback_data=f"index_backfill:{channel_id}")],
                [InlineKeyboardButton("Replace placeholders", callback_data=f"index_targets:{channel_id}:replace"),
                 InlineKeyboardButton("Add placeholder", callback_data=f"index_targets:{channel_id}:add")],
                [InlineKeyboardButton(f"Clear placeholders ({len(targets)})", callback_data=f"index_clear:{channel_id}")],
            ])
        else:
            rows.append([InlineKeyboardButton("Register index placeholders", callback_data=f"index_targets:{channel_id}:replace")])
        rows.append([InlineKeyboardButton("← Index settings", callback_data=f"index_choose:{channel_id}:open:-")])
        return InlineKeyboardMarkup(rows)

    async def _show_index_settings(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: int
    ) -> None:
        channel = await self._private_channel_target(update, context, str(channel_id))
        if channel is None:
            return
        repository = self._get_channel_index_repository()
        if repository is None:
            await self._send_or_edit(update, "Channel indexes need MongoDB storage. Configure MONGODB_URI first.", self._misc_keyboard())
            return
        config = await asyncio.to_thread(repository.get, channel_id)
        config = config or {}
        enabled = bool(config.get("enabled"))
        mode = str(config.get("entry_mode") or ("prefix" if config.get("title_mode") == "prefix" else "text"))
        sort_order = str(config.get("sort_order") or "added")
        bullet = str(config.get("entry_bullet") or "🔹")
        prefix = str(config.get("entry_prefix") or config.get("title_prefix") or "")
        source_label = {
            "text": "Text", "hashtags": "Hashtags", "links": "Links",
            "image": "Image", "file": "File", "prefix": "Custom prefix",
        }.get(mode, "Text")
        if mode == "prefix" and prefix:
            source_label = f"{source_label} ({prefix})"
        targets = len(config.get("targets") or [])
        excluded_senders = len(config.get("excluded_sender_ids") or [])
        text = (
            f"<b>📇 Index · {html_escape(channel.title or 'Channel')}</b>\n\n"
            "Annie adds links to the index messages you register. She can fix links when posts change and remove them when posts are deleted.\n\n"
            f"Status: <b>{'On' if enabled else 'Off'}</b> · Delay: <b>{int(config.get('delay_minutes') or 5)} min</b>\n"
            f"Label: <b>{html_escape(source_label)}</b> · Order: <b>{'A–Z' if sort_order == 'alphabetical' else 'Added'}</b>\n"
            f"Placeholders: <b>{targets}</b> · Excluded senders: <b>{excluded_senders}</b>\n\n"
            "• <b>Turn updates on/off:</b> start or pause automatic index updates.\n"
            "• <b>1 / 5 / 10 min:</b> choose how long Annie waits before adding a link.\n"
            "• <b>Index Format:</b> choose the link label, bullet, and entry order.\n"
            "• <b>Index lists:</b> register the index messages Annie can edit; set list markers or import older posts.\n"
            "• <b>Excluded senders:</b> skip posts from selected users or bots."
        )
        await self._send_or_edit(update, text, self._index_settings_keyboard(channel_id, config))

    async def _show_index_format(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: int
    ) -> None:
        channel = await self._private_channel_target(update, context, str(channel_id))
        if channel is None:
            return
        repository = self._get_channel_index_repository()
        config = await asyncio.to_thread(repository.get, channel_id) if repository else None
        config = config or {}
        mode = str(config.get("entry_mode") or "text")
        source_labels = {
            "text": "Text line", "hashtags": "One hashtag", "links": "First link",
            "image": "Image caption", "file": "File name", "prefix": "Custom prefix",
        }
        source = source_labels.get(mode, "Text line")
        if mode == "prefix":
            prefix = str(config.get("entry_prefix") or config.get("title_prefix") or "")
            source = f"Custom prefix: {prefix}" if prefix else "Custom prefix (not set)"
        bullet = str(config.get("entry_bullet") or "🔹")
        order = "A–Z" if config.get("sort_order") == "alphabetical" else "Order added"
        prefix_help = (
            "• <b>Set prefix:</b> change the marker Annie looks for, such as <code>Title:</code>.\n"
            if mode == "prefix" else ""
        )
        text = (
            f"<b>Index Format · {html_escape(channel.title or 'Channel')}</b>\n\n"
            "Choose what Annie writes as each clickable index entry.\n\n"
            f"Current label: <b>{html_escape(source)}</b> · Order: <b>{order}</b>\n"
            f"Default bullet: <b>{html_escape(bullet)}</b>\n\n"
            "• <b>Text line:</b> use the first non-empty text line.\n"
            "• <b>One #hashtag:</b> use the first hashtag.\n"
            "• <b>First link:</b> use the first URL’s site and path as the label.\n"
            "• <b>Image caption:</b> use its first caption line; without one, the label is “Image.”\n"
            "• <b>File name:</b> use the file name, or its caption if unnamed.\n"
            "• <b>Custom prefix:</b> use the text after your marker, such as <code>Title:</code>.\n"
            f"{prefix_help}"
            "• <b>A–Z / Order added:</b> sort links by name or by arrival time.\n"
            "• <b>Set default bullet:</b> change the symbol before each link. A placeholder can have its own bullet.\n"
            "Each entry opens the original post."
        )
        await self._send_or_edit(update, text, self._index_format_keyboard(channel_id, config))

    async def _show_index_lists(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: int
    ) -> None:
        channel = await self._private_channel_target(update, context, str(channel_id))
        if channel is None:
            return
        repository = self._get_channel_index_repository()
        config = await asyncio.to_thread(repository.get, channel_id) if repository else None
        config = config or {}
        targets = list(config.get("targets") or [])
        if targets:
            description = (
                "• <b>Set list markers:</b> match text or a hashtag in posts to the right list. Posts with no match go to an unmarked list.\n"
                "• <b>Set placeholder bullet:</b> change the symbol for one index message.\n"
                "• <b>Import older posts:</b> forward old channel posts to add their links.\n"
                "• <b>Add placeholder:</b> keep current index messages and register more.\n"
                "• <b>Replace placeholders:</b> use the forwarded messages instead of the current ones.\n"
                "• <b>Clear placeholders:</b> stop updating registered messages; it does not delete them."
            )
        else:
            description = (
                "• <b>Register placeholders:</b> forward the existing text messages you want Annie to update.\n"
                "• Forward several together or one at a time, then tap Done to register them."
            )
        text = (
            f"<b>Index lists · {html_escape(channel.title or 'Channel')}</b>\n\n"
            "Choose which existing messages Annie can update as your indexes.\n\n"
            f"Registered index messages: <b>{len(targets)}</b>\n\n"
            f"{description}"
        )
        await self._send_or_edit(update, text, self._index_lists_keyboard(channel_id, config))

    async def _show_index_exclusions(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: int
    ) -> None:
        channel = await self._private_channel_target(update, context, str(channel_id))
        if channel is None:
            return
        repository = self._get_channel_index_repository()
        config = await asyncio.to_thread(repository.get, channel_id) if repository else None
        labels = (config or {}).get("excluded_sender_labels") or {}
        sender_ids = sorted({
            int(sender_id)
            for sender_id in (config or {}).get("excluded_sender_ids") or []
            if str(sender_id).lstrip("+").isdigit() and int(sender_id) > 0
        })
        rows = [[InlineKeyboardButton(
            f"Remove {(str(labels.get(str(sender_id)) or sender_id))[:40]}",
            callback_data=f"index_exclusion_remove:{channel_id}:{sender_id}",
        )] for sender_id in sender_ids]
        rows.append([InlineKeyboardButton(
            "Add sender", callback_data=f"index_exclusion_add:{channel_id}"
        )])
        rows.append([InlineKeyboardButton(
            "← Index settings", callback_data=f"index_choose:{channel_id}:open:-"
        )])
        listing = "\n".join(
            f"• {html_escape(str(labels.get(str(sender_id)) or 'Sender'))} "
            f"(<code>{sender_id}</code>)"
            for sender_id in sender_ids
        ) or "• None"
        text = (
            f"<b>Excluded senders · {html_escape(channel.title or 'Channel')}</b>\n\n"
            "Use this list to stop selected people or bots from being added to the Index.\n\n"
            "• <b>Add sender:</b> forward one of their channel posts, enter their public <code>@username</code>, or enter the sender ID from Annie’s log.\n"
            "• <b>Remove sender:</b> allow their future posts to be indexed again.\n\n"
            f"{listing}"
        )
        await self._send_or_edit(update, text, InlineKeyboardMarkup(rows))

    async def _index_text_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or update.effective_message is None:
            return
        if self._channel_manager.has_pending_input(update.effective_user.id):
            return
        if update.effective_chat is None or update.effective_chat.type != "private":
            return
        user_id = update.effective_user.id
        state = self._index_pending_input.get(user_id)
        if not state or state.get("expires_at", 0) < time.time():
            self._index_pending_input.pop(user_id, None)
            return
        input_kind = state.get("kind")
        if input_kind == "bullet":
            bullet = (update.effective_message.text or "").strip()
            if not bullet or len(bullet) > 12 or "\n" in bullet or "\r" in bullet:
                await update.effective_message.reply_text(
                    "Send one emoji or symbol, up to 12 characters, such as • or 📚."
                )
                return
            channel = await self._private_channel_target(update, context, str(state["channel_id"]))
            if channel is None:
                self._index_pending_input.pop(user_id, None)
                return
            repository = self._get_channel_index_repository()
            if repository:
                target_id = state.get("target_id")
                if target_id is None:
                    await asyncio.to_thread(
                        repository.update, state["channel_id"], {"entry_bullet": bullet}
                    )
                else:
                    config = await asyncio.to_thread(repository.get, int(state["channel_id"])) or {}
                    targets = list(config.get("targets") or [])
                    matched = False
                    for target in targets:
                        if int(target.get("message_id", -1)) == int(target_id):
                            target["entry_bullet"] = bullet
                            matched = True
                            break
                    if not matched:
                        await update.effective_message.reply_text(
                            "That placeholder is no longer registered. Please select it again."
                        )
                        self._index_pending_input.pop(user_id, None)
                        return
                    await asyncio.to_thread(
                        repository.update, int(state["channel_id"]), {"targets": targets}
                    )
                await self._refresh_index_posts(context.bot, state["channel_id"])
            self._index_pending_input.pop(user_id, None)
            if state.get("target_id") is not None:
                await self._show_index_lists(update, context, state["channel_id"])
            else:
                await self._show_index_format(update, context, state["channel_id"])
            return
        if input_kind == "category":
            marker = (update.effective_message.text or "").strip()
            if not marker or len(marker) > 60 or "\n" in marker or "\r" in marker:
                await update.effective_message.reply_text(
                    "Send one short marker found in posts for this list, such as #Manga."
                )
                return
            channel = await self._private_channel_target(update, context, str(state["channel_id"]))
            if channel is None:
                self._index_pending_input.pop(user_id, None)
                return
            repository = self._get_channel_index_repository()
            if repository:
                config = await asyncio.to_thread(repository.get, int(state["channel_id"])) or {}
                targets = list(config.get("targets") or [])
                target_id = int(state["target_id"])
                for target in targets:
                    if int(target.get("message_id", -1)) == target_id:
                        target["category_marker"] = marker
                        break
                await asyncio.to_thread(repository.update, int(state["channel_id"]), {"targets": targets})
            self._index_pending_input.pop(user_id, None)
            await self._show_index_lists(update, context, state["channel_id"])
            return
        if input_kind == "exclude_sender":
            channel = await self._private_channel_target(update, context, str(state["channel_id"]))
            if channel is None:
                self._index_pending_input.pop(user_id, None)
                return
            repository = self._get_channel_index_repository()
            if repository is None:
                self._index_pending_input.pop(user_id, None)
                await update.effective_message.reply_text("Channel indexes need MongoDB storage.")
                return
            raw_sender = (update.effective_message.text or "").strip()
            sender_label = None
            if raw_sender.startswith("@"):
                if len(raw_sender) > 33 or len(raw_sender) < 6:
                    await update.effective_message.reply_text(
                        "Send a public username such as @postbot, or a positive sender ID."
                    )
                    return
                if self._channel_index_runtime is None and not await self._ensure_channel_index_listener():
                    await update.effective_message.reply_text(
                        "I couldn’t look up that username. Try a sender ID from Annie’s log instead."
                    )
                    return
                try:
                    sender_id, sender_label = await self._channel_index_runtime.resolve_sender_username(raw_sender)
                except Exception as exc:
                    logger.info("Could not resolve excluded sender username error=%s", type(exc).__name__)
                    sender_id, sender_label = None, None
                await self._stop_channel_index_listener_if_idle()
                if sender_id is None:
                    await update.effective_message.reply_text(
                        "I couldn’t find that public user or bot. Check the username and try again."
                    )
                    return
            else:
                try:
                    sender_id = int(raw_sender)
                except ValueError:
                    sender_id = 0
                if sender_id <= 0 or len(raw_sender.lstrip("+")) > 20:
                    await update.effective_message.reply_text(
                        "Send a public @username or the positive sender ID from Annie’s log, such as 273234066."
                    )
                    return
            await asyncio.to_thread(
                repository.add_excluded_sender, int(state["channel_id"]), sender_id, sender_label
            )
            self._index_pending_input.pop(user_id, None)
            await self._show_index_exclusions(update, context, int(state["channel_id"]))
            return
        if input_kind != "prefix":
            return
        prefix = (update.effective_message.text or "").strip()
        if not prefix or len(prefix) > 40 or "\n" in prefix:
            await update.effective_message.reply_text("Send one title prefix of 1–40 characters, such as Title:")
            return
        channel = await self._private_channel_target(update, context, str(state["channel_id"]))
        if channel is None:
            self._index_pending_input.pop(user_id, None)
            return
        repository = self._get_channel_index_repository()
        if repository:
            await asyncio.to_thread(repository.update, state["channel_id"], {"entry_mode": "prefix", "entry_prefix": prefix})
        self._index_pending_input.pop(user_id, None)
        await self._show_index_format(update, context, state["channel_id"])

    async def _show_index_categories(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: int
    ) -> None:
        channel = await self._private_channel_target(update, context, str(channel_id))
        if channel is None:
            return
        repository = self._get_channel_index_repository()
        config = await asyncio.to_thread(repository.get, channel_id) if repository else None
        targets = list((config or {}).get("targets") or [])
        rows = []
        for index, target in enumerate(targets, start=1):
            target_id = int(target["message_id"])
            marker = str(target.get("category_marker") or "").strip()
            base_text = re.sub(r"<[^>]*>", "", str(target.get("base_html") or ""))
            base_text = re.sub(r"\s+", " ", html_unescape(base_text)).strip()
            label = f"{index}. {base_text[:22] or 'Index post'}"
            rows.append([InlineKeyboardButton(
                f"{label} · {marker[:24] if marker else 'All posts'}",
                callback_data=f"index_category_set:{channel_id}:{target_id}",
            )])
            if marker:
                rows.append([InlineKeyboardButton(
                    f"Clear marker for list {index}",
                    callback_data=f"index_category_clear:{channel_id}:{target_id}",
                )])
        rows.append([InlineKeyboardButton("← Index lists", callback_data=f"index_lists:{channel_id}")])
        await self._send_or_edit(
            update,
            f"<b>List markers · {html_escape(channel.title or 'Channel')}</b>\n\n"
            "Choose a list below, then send a short marker, such as <code>#Manga</code>.\n\n"
            "• Posts with that marker go to the matching index message.\n"
            "• Posts without a matching marker go to an unmarked list.\n"
            "• Tap <b>Clear marker</b> to remove a list’s marker.",
            InlineKeyboardMarkup(rows),
        )

    async def _refresh_index_posts(self, bot: Any, channel_id: int) -> None:
        repository = self._get_channel_index_repository()
        if repository is None:
            return
        config = await asyncio.to_thread(repository.get, int(channel_id)) or {}
        entries = list(config.get("entries") or [])
        for target in config.get("targets") or []:
            target_id = int(target["message_id"])
            target_entries = [
                entry for entry in entries
                if int(entry.get("target_message_id", -1)) == target_id
            ]
            text = ChannelIndexRuntime._render_target(
                str(target.get("base_html") or ""), target_entries,
                str(config.get("sort_order") or "added"),
                str(target.get("entry_bullet") or config.get("entry_bullet") or "🔹"),
            )
            try:
                await bot.edit_message_text(
                    chat_id=int(channel_id), message_id=target_id, text=text,
                    parse_mode="HTML", disable_web_page_preview=True,
                )
            except BadRequest as exc:
                if "message is not modified" not in str(exc).casefold():
                    logger.warning(
                        "[channel-index] could not refresh index post channel_id=%s index_post_id=%s error=%s",
                        channel_id, target_id, type(exc).__name__,
                    )
            except Exception as exc:
                logger.warning(
                    "[channel-index] could not refresh index post channel_id=%s index_post_id=%s error=%s",
                    channel_id, target_id, type(exc).__name__,
                )

    async def _index_forwarded_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or update.effective_message is None:
            return
        if self._channel_manager.has_pending_input(update.effective_user.id):
            return
        user_id = update.effective_user.id
        state = self._index_pending_input.get(user_id)
        if not state or state.get("kind") not in {"targets", "backfill", "bullet_target", "exclude_sender"} or state.get("expires_at", 0) < time.time():
            return
        message = update.effective_message
        channel = await self._private_channel_target(update, context, str(state["channel_id"]))
        if channel is None:
            self._index_pending_input.pop(user_id, None)
            return
        origin = getattr(message, "forward_origin", None)
        if getattr(origin, "type", None) != "channel":
            await message.reply_text("Please forward an index post from the channel you selected.")
            return
        channel_id = int(state["channel_id"])
        if int(origin.chat.id) != channel_id:
            await message.reply_text("That post is from a different channel. Forward a post from the selected channel.")
            return
        if state.get("kind") == "exclude_sender":
            if self._get_channel_index_repository() is None:
                self._index_pending_input.pop(user_id, None)
                await message.reply_text("Channel indexes need MongoDB storage.")
                return
            if self._channel_index_runtime is None and not await self._ensure_channel_index_listener():
                await message.reply_text(
                    "I couldn’t check that post’s sender. Send the positive sender ID from Annie’s log instead."
                )
                return
            runtime = self._channel_index_runtime
            try:
                sender_id, sender_label = await runtime.get_sender_details(
                    channel_id, int(origin.message_id)
                )
            except Exception as exc:
                logger.warning(
                    "[channel-index] sender lookup failed channel_id=%s post_id=%s error=%s",
                    channel_id, int(origin.message_id), type(exc).__name__,
                )
                sender_id, sender_label = None, None
            if sender_id is None or sender_id <= 0:
                await message.reply_text(
                    "I couldn’t identify the sender. Turn on the channel’s sender profile, then forward one of their posts again."
                )
                await self._stop_channel_index_listener_if_idle()
                return
            repository = self._get_channel_index_repository()
            await asyncio.to_thread(
                repository.add_excluded_sender, channel_id, sender_id, sender_label
            )
            self._index_pending_input.pop(user_id, None)
            label = f" ({html_escape(sender_label)})" if sender_label else ""
            await message.reply_text(
                f"Posts from sender <code>{sender_id}</code>{label} will now be skipped.",
                parse_mode=ParseMode.HTML,
            )
            await self._show_index_exclusions(update, context, channel_id)
            await self._stop_channel_index_listener_if_idle()
            return
        if state.get("kind") == "bullet_target":
            repository = self._get_channel_index_repository()
            config = await asyncio.to_thread(repository.get, channel_id) if repository else None
            target_id = int(origin.message_id)
            if not any(int(target.get("message_id", -1)) == target_id
                       for target in (config or {}).get("targets") or []):
                await message.reply_text("That post isn’t a registered Index placeholder. Forward one of your registered placeholders.")
                return
            state.update({"kind": "bullet", "target_id": target_id,
                          "expires_at": time.time() + 600})
            await message.reply_text(
                "Send one emoji or symbol for this placeholder only, such as • or 📚."
            )
            return
        if state.get("kind") == "backfill":
            runtime = self._channel_index_runtime
            if runtime is None:
                await message.reply_text("The index listener is not running. Turn updates off and on, then try again.")
                return
            result = await runtime.import_forwarded_post(
                channel_id, int(origin.message_id), message
            )
            responses = {
                "added": "Added to the Index. Forward another older post, or tap Done.",
                "queued": "Saved; waiting for a free index placeholder. Forward another, or tap Done.",
                "duplicate": "That post is already indexed or queued. Forward another, or tap Done.",
                "no_title": "Couldn’t find an entry label in that post. Check the entry source setting, then forward another.",
                "unmatched": "No list marker matched that post. Forward another, or tap Done.",
                "ambiguous": "That post matches more than one list marker, so Annie skipped it. Forward another, or tap Done.",
                "excluded": "That post is from an excluded sender, so Annie skipped it. Forward another, or tap Done.",
                "sender_check_failed": "Annie couldn’t check who posted that. Please try again later.",
                "unavailable": "Turn Index updates on and register a placeholder first.",
            }
            await message.reply_text(
                responses.get(result, "Could not import that post. Forward another or tap Done."),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                    "Done", callback_data=f"index_import_done:{channel_id}"
                )]]),
            )
            return
        base_html = getattr(message, "text_html", None) or getattr(message, "caption_html", None)
        if not base_html:
            await message.reply_text("That index post has no text or caption I can update. Forward a text-based index post instead.")
            return
        target = {"message_id": int(origin.message_id), "base_html": base_html}
        if len(base_html.encode("utf-16-le")) // 2 > 4096:
            await message.reply_text("That index is too long to edit as one Telegram message.")
            return
        targets = state.setdefault("targets", [])
        if len(targets) >= 12 and all(int(item["message_id"]) != target["message_id"] for item in targets):
            await message.reply_text("A maximum of 12 index posts can be registered.")
            return
        if all(int(item["message_id"]) != target["message_id"] for item in targets):
            targets.append(target)
        await message.reply_text(
            f"Received {len(targets)} placeholder(s). Forward more, or tap Done to register them.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                "Done", callback_data=f"index_targets_done:{channel_id}"
            )]]),
        )

    async def _connected_channels_administered_by(
        self, bot, user_id: int, channels: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Check channel admin membership concurrently to avoid serial API waits."""
        semaphore = asyncio.Semaphore(8)

        async def check(channel: dict[str, Any]) -> dict[str, Any] | None:
            try:
                async with semaphore:
                    member = await bot.get_chat_member(int(channel["id"]), int(user_id))
            except Exception:
                return None
            status = getattr(member, "status", "")
            status = str(getattr(status, "value", status))
            return channel if status in {"administrator", "creator"} else None

        checked = await asyncio.gather(*(check(channel) for channel in channels))
        return [channel for channel in checked if channel is not None]

    async def _connected_channel_names(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> str:
        repository = self._get_channel_connection_repository()
        if repository is None:
            return "Channel connections need cloud storage. Configure MONGODB_URI, then try again."
        try:
            channels = await asyncio.to_thread(repository.list_channels)
        except Exception as exc:
            logger.warning("Could not list channel connections error=%s", type(exc).__name__)
            return "I couldn’t load connected channels. Please try again."
        eligible = await self._connected_channels_administered_by(
            context.bot, update.effective_user.id, channels
        )
        names = [html_escape(str(channel["name"])) for channel in eligible]
        return "You are connected to:\n" + "\n".join(
            f"• {name}" for name in names
        ) if names else "You are not connected to any channels."

    async def _show_disconnect_picker(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        notice: str | None = None,
    ) -> None:
        if update.effective_chat is None or update.effective_chat.type != "private":
            await self._send_or_edit(update, "Use /disconnect in a private chat with Annie.")
            return
        repository = self._get_channel_connection_repository()
        if repository is None:
            await self._send_or_edit(
                update, "Channel connections need cloud storage. Configure MONGODB_URI first.",
                self._misc_keyboard(),
            )
            return
        try:
            channels = await asyncio.to_thread(repository.list_channels)
        except Exception as exc:
            logger.warning("Could not list channels for disconnect error=%s", type(exc).__name__)
            await self._send_or_edit(update, "I couldn’t load connected channels. Please try again.")
            return
        eligible = await self._connected_channels_administered_by(
            context.bot, update.effective_user.id, channels
        )
        if not eligible:
            text = (notice + "\n\n" if notice else "") + "You are not connected to any channels."
            await self._send_or_edit(
                update, text,
                InlineKeyboardMarkup([[InlineKeyboardButton(
                    "← Channel Manager", callback_data="cm:home"
                )]]),
            )
            return
        rows = [[InlineKeyboardButton(
            str(channel["name"])[:60], callback_data=f"disconnect_channel:{int(channel['id'])}"
        )] for channel in eligible]
        rows.append([InlineKeyboardButton("← Channel Manager", callback_data="cm:home")])
        text = (notice + "\n\n" if notice else "") + (
            "<b>Connected channels</b>\nTap a channel to disconnect it:"
        )
        await self._send_or_edit(update, text, InlineKeyboardMarkup(rows))

    async def _private_channel_target(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, raw_id: str
    ):
        """Resolve a channel ID and verify the caller is one of its admins."""
        chat = update.effective_chat
        if chat is None or chat.type != "private":
            await update.effective_message.reply_text(
                "Use this command in a private chat with Annie."
            )
            return None
        try:
            identifier = raw_id.strip()
            channel_id = int(identifier) if not identifier.startswith("@") else None
        except (TypeError, ValueError):
            await update.effective_message.reply_text(
                "Use a numeric channel ID or a public channel username such as <code>@mychannel</code>.",
                parse_mode=ParseMode.HTML,
            )
            return None
        if channel_id is not None and channel_id >= 0:
            await update.effective_message.reply_text(
                "Channel IDs are negative numbers. Send the full ID, including its minus sign."
            )
            return None
        try:
            channel = await context.bot.get_chat(identifier if channel_id is None else channel_id)
            if getattr(channel, "type", None) != "channel":
                await update.effective_message.reply_text(
                    "That username or ID belongs to a chat, not a Telegram channel."
                )
                return None
            member = await context.bot.get_chat_member(channel.id, update.effective_user.id)
        except BadRequest as exc:
            logger.info(
                "Channel connection lookup failed channel_id=%s error=%s",
                channel_id, type(exc).__name__,
            )
            await update.effective_message.reply_text(
                "I couldn’t access that channel. Check the ID and add Annie as a channel administrator first."
            )
            return None
        except Exception as exc:
            logger.warning(
                "Could not verify channel admin channel_id=%s error=%s",
                channel_id, type(exc).__name__,
            )
            await update.effective_message.reply_text(
                "I couldn’t verify your channel access just now. Please try again."
            )
            return None
        if getattr(member, "status", "") not in ("administrator", "creator"):
            await update.effective_message.reply_text(
                "Only that channel’s owner or an administrator can manage its connection."
            )
            return None
        return channel

    async def connect_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Connect a channel after verifying the caller and bot permissions."""
        message = update.effective_message
        if update.effective_chat is None or update.effective_chat.type != "private":
            await message.reply_text("Use /connect in a private chat with Annie.")
            return
        if len(context.args) != 1:
            await message.reply_text("Usage: /connect <channel_id or @public_channel>")
            return
        channel = await self._private_channel_target(update, context, context.args[0])
        if channel is None:
            return
        try:
            bot_member = await context.bot.get_chat_member(channel.id, context.bot.id)
        except Exception as exc:
            logger.warning(
                "Could not verify bot channel admin channel_id=%s error=%s",
                channel.id, type(exc).__name__,
            )
            await message.reply_text("I couldn’t verify my channel access. Please try again.")
            return
        if getattr(bot_member, "status", "") not in ("administrator", "creator"):
            await message.reply_text(
                "Add Annie as an administrator in that channel before connecting it."
            )
            return
        repository = self._get_channel_connection_repository()
        if repository is None:
            await message.reply_text(
                "Channel connections need cloud storage. Configure MONGODB_URI, then try again."
            )
            return
        try:
            created = await asyncio.to_thread(
                repository.connect, channel.id, channel.title or "Telegram channel",
                update.effective_user.id,
            )
        except Exception as exc:
            logger.warning(
                "Could not save channel connection channel_id=%s error=%s",
                channel.id, type(exc).__name__,
            )
            await message.reply_text("I couldn’t save that channel connection. Please try again.")
            return
        self._channel_manager.invalidate_access_cache(update.effective_user.id)
        await message.reply_text(
            f"Connected <b>{html_escape(channel.title or 'Telegram channel')}</b>."
            if created else
            f"<b>{html_escape(channel.title or 'Telegram channel')}</b> is already connected.",
            parse_mode=ParseMode.HTML,
        )
        self._schedule_user_command_scope_refresh(
            context.bot, update.effective_user.id, force=True
        )

    async def connections_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """List connected channels by name only; private chat only."""
        message = update.effective_message
        if update.effective_chat is None or update.effective_chat.type != "private":
            await message.reply_text("Use /connections in a private chat with Annie.")
            return
        if context.args:
            await message.reply_text("Usage: /connections")
            return
        await message.reply_text(
            await self._connected_channel_names(update, context),
            parse_mode=ParseMode.HTML,
        )

    async def disconnect_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show connected channels and let an admin choose one to disconnect."""
        message = update.effective_message
        if update.effective_chat is None or update.effective_chat.type != "private":
            await message.reply_text("Use /disconnect in a private chat with Annie.")
            return
        if context.args:
            await message.reply_text("Use /disconnect with no arguments, then tap the channel to disconnect.")
            return
        await self._show_disconnect_picker(update, context)

    @staticmethod
    def _features_text() -> str:
        return """<b>✦ What Annie can do?</b>

📖 <b>Find books</b>
Search by title or author in a chat or inline.

🪄 <b>Explore details</b>
See available covers, descriptions, genres, publication information, and ratings.

🖼 <b>Keep a cover</b>
Download a book cover from its details.

🧭 <b>Discover in the portal</b>
Browse trending picks, genre shelves, search, and similar books.

✨ <b>Find your next read</b>
Get recommendations from books you’ve read or liked, genres, and moods.

📚 <b>Build your Bookshelf</b>
Save books to My Books or move favourites into their own list. Search, sort, and choose grid or list view for each section; your bookshelf can sync to your Telegram account."""

    async def _send_mini_app(self, update: Update, context: ContextTypes.DEFAULT_TYPE, page: str = "") -> None:
        markup = self._mini_app_markup(update, context, page)
        if markup is None:
            await update.effective_message.reply_text(
                "Annie Search isn’t connected yet. Set ANNIE_APP_URL to the public HTTPS address of the Mini App."
            )
            return
        prompt = "Open Annie Search to explore books."
        if page == "recommendations":
            prompt = "Open Annie’s recommendation studio and tell her what you like."
        elif page == "bookshelf":
            prompt = "Open your Bookshelf to browse My Books and Favourites."
        elif page == "favorites":
            prompt = "Open your Favourites."
        await update.effective_message.reply_text(prompt, reply_markup=markup)

    async def portal_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Open the Annie Search Mini App from a regular bot chat."""
        await self._send_mini_app(update, context)

    async def recom_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Open the recommendations screen directly inside the Mini App."""
        await self._send_mini_app(update, context, "recommendations")

    async def bookshelf_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Open My Bookshelf directly inside the Mini App."""
        await self._send_mini_app(update, context, "bookshelf")

    async def favorites_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Open the Favourites tab directly inside the Mini App."""
        await self._send_mini_app(update, context, "favorites")

    def _is_bot_owner(self, update: Update) -> bool:
        return bool(
            self._owner_user_id is not None
            and update.effective_user is not None
            and update.effective_user.id == self._owner_user_id
        )

    async def _reply_owner_command_denied(self, update: Update) -> None:
        message = update.effective_message
        if self._owner_user_id is None:
            await message.reply_text("Owner commands are disabled: BOT_OWNER_ID is not configured.")
        else:
            await message.reply_text("This command is only available to the bot owner.")

    async def _admin_command_target(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
        if not self._is_bot_owner(update):
            await self._reply_owner_command_denied(update)
            return None
        if len(context.args) != 1:
            await update.effective_message.reply_text("Usage: /authorize <telegram_user_id>")
            return None
        try:
            user_id = int(context.args[0])
        except (TypeError, ValueError):
            user_id = 0
        if not 0 < user_id <= 2**63 - 1:
            await update.effective_message.reply_text("Enter a valid positive Telegram user ID.")
            return None
        if user_id == self._owner_user_id:
            await update.effective_message.reply_text("The bot owner is already exempt from restrictions.")
            return None
        return user_id

    async def authorize_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Add one Telegram user ID to the owner-managed exemption list."""
        user_id = await self._admin_command_target(update, context)
        if user_id is None:
            return
        if not await self._load_bot_admin_ids(force=True):
            message = (
                "Admin storage is unavailable. Configure MONGODB_URI, then try again."
                if not os.getenv("MONGODB_URI", "").strip()
                else "Could not reach MongoDB. No change was made; try again shortly."
            )
            await update.effective_message.reply_text(message)
            return
        repository = self._get_bot_admin_repository()
        try:
            inserted = await asyncio.to_thread(
                repository.authorize, user_id, self._owner_user_id
            )
        except Exception as exc:
            logger.warning("Could not authorize bot user (%s)", type(exc).__name__)
            await update.effective_message.reply_text("Could not reach MongoDB. No change was made; try again shortly.")
            return
        self._authorized_bot_admin_ids.add(user_id)
        self._bot_admin_cache_loaded = True
        self._bot_admin_cache_expires_at = time.monotonic() + 60
        await update.effective_message.reply_text(
            f"User <code>{user_id}</code> is now authorized for cooldown and block exemptions."
            if inserted else f"User <code>{user_id}</code> is already authorized.",
            parse_mode=ParseMode.HTML,
        )

    async def unauthorize_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Remove one Telegram user ID from the owner-managed exemption list."""
        user_id = await self._admin_command_target(update, context)
        if user_id is None:
            return
        if not await self._load_bot_admin_ids(force=True):
            message = (
                "Admin storage is unavailable. Configure MONGODB_URI, then try again."
                if not os.getenv("MONGODB_URI", "").strip()
                else "Could not reach MongoDB. No change was made; try again shortly."
            )
            await update.effective_message.reply_text(message)
            return
        repository = self._get_bot_admin_repository()
        try:
            removed = await asyncio.to_thread(repository.unauthorize, user_id)
        except Exception as exc:
            logger.warning("Could not revoke bot user authorization (%s)", type(exc).__name__)
            await update.effective_message.reply_text("Could not reach MongoDB. No change was made; try again shortly.")
            return
        self._authorized_bot_admin_ids.discard(user_id)
        self._bot_admin_cache_loaded = True
        self._bot_admin_cache_expires_at = time.monotonic() + 60
        await update.effective_message.reply_text(
            f"Authorization removed for <code>{user_id}</code>."
            if removed else f"User <code>{user_id}</code> was not on the authorized list.",
            parse_mode=ParseMode.HTML,
        )

    async def admins_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """List the owner and authorized users; available only to the owner."""
        if not self._is_bot_owner(update):
            await self._reply_owner_command_denied(update)
            return
        if context.args:
            await update.effective_message.reply_text("Usage: /admins")
            return
        if not await self._load_bot_admin_ids(force=True):
            message = (
                "Admin storage is unavailable. Configure MONGODB_URI, then try again."
                if not os.getenv("MONGODB_URI", "").strip()
                else "Could not reach MongoDB. Try again shortly."
            )
            await update.effective_message.reply_text(message)
            return
        owner_id = self._owner_user_id
        owner_user = update.effective_user
        admin_ids = sorted(
            user_id for user_id in self._authorized_bot_admin_ids
            if user_id != owner_id
        )

        async def fetch_profile(user_id: int):
            try:
                return await context.bot.get_chat(user_id)
            except Exception as exc:
                logger.debug(
                    "Could not fetch Telegram profile for bot admin %s (%s)",
                    user_id, type(exc).__name__,
                )
                return None

        profiles = await asyncio.gather(*(fetch_profile(user_id) for user_id in admin_ids))
        entries = [(owner_id, owner_user, "Owner")]
        entries.extend((user_id, profile, "Admin") for user_id, profile in zip(admin_ids, profiles))

        lines = ["👥 <b>Admin Management</b>", f"Total admins: {len(entries)}", ""]
        for index, (user_id, profile, role) in enumerate(entries, start=1):
            first_name = str(getattr(profile, "first_name", "") or "").strip()
            last_name = str(getattr(profile, "last_name", "") or "").strip()
            display_name = " ".join(part for part in (first_name, last_name) if part)
            username = str(getattr(profile, "username", "") or "").strip().lstrip("@")
            if not display_name:
                display_name = "Telegram user"
            if username:
                safe_username = html_escape(username)
                name_line = (
                    f"<b>{html_escape(display_name)}</b> "
                    f"(<a href=\"https://t.me/{safe_username}\">@{safe_username}</a>)"
                )
            else:
                name_line = f"<b>{html_escape(display_name)}</b>"
            role_line = "👑 Owner" if role == "Owner" else "✅ Admin"
            lines.extend((
                f"{index}. {name_line}",
                f"   {role_line}",
                f"   ID: <code>{user_id}</code>",
                "",
            ))
        lines.append("<i>Use /authorize and /unauthorize to manage access.</i>")
        await update.effective_message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send the concise welcome and action menu on /start."""
        start_parameter = context.args[0] if context.args else ""
        page_parameters = {
            "portal": "", "recom": "recommendations",
            "bookshelf": "bookshelf", "favorites": "favorites",
        }
        if start_parameter in page_parameters:
            page = page_parameters[start_parameter]
            await self._send_mini_app(update, context, page)
            return
        show_channel_manager = await self._channel_manager.should_show_button(update, context.bot)
        if update.effective_chat and update.effective_chat.type == "private":
            self._schedule_user_command_scope_refresh(
                context.bot, update.effective_user.id
            )
        await update.message.reply_text(
            self._start_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=self._start_keyboard(
                update, context, show_channel_manager,
            ),
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send help text on /help."""
        await update.message.reply_text(
            self._help_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=self._help_keyboard(),
        )

    async def ping_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Report process uptime and basic configured runtime status."""
        ping_started = time.perf_counter()
        pong_message = await update.message.reply_text("🏓 Pong…")
        latency_ms = round((time.perf_counter() - ping_started) * 1000)
        uptime_seconds = max(0, int(time.time() - getattr(self, "_started_at", time.time())))
        days, remainder = divmod(uptime_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_parts = []
        if days:
            uptime_parts.append(f"{days}d")
        if hours or days:
            uptime_parts.append(f"{hours}h")
        if minutes or hours or days:
            uptime_parts.append(f"{minutes}m")
        uptime_parts.append(f"{seconds}s")
        mode = "Webhook" if getattr(self, "webhook_mode", False) else "Polling"
        mini_app = "Configured" if self._mini_app_url() else "Not configured"
        await pong_message.edit_text(
            f"<b>🏓 Pong: {latency_ms} ms</b>\n"
            "<b>✅ Annie is online</b>\n\n"
            f"⏱ <b>Uptime:</b> {' '.join(uptime_parts)}\n"
            f"🔌 <b>Connection:</b> {mode}\n"
            f"📱 <b>Mini App URL:</b> {mini_app}",
            parse_mode=ParseMode.HTML,
        )

    async def id_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show the ID of the current Telegram chat without publishing a menu entry."""
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None:
            return
        chat_type = str(getattr(chat.type, "value", chat.type))
        chat_name = (
            getattr(chat, "title", None)
            or " ".join(filter(None, [getattr(chat, "first_name", None), getattr(chat, "last_name", None)]))
            or "Private chat"
        )
        text = (
            f"🆔 <b>{html_escape(chat_name)}</b>\n"
            f"Chat ID: <code>{int(chat.id)}</code>"
        )
        if chat_type == "private":
            text += "\nRun /id inside a channel to get its channel ID."
        await message.reply_text(text, parse_mode=ParseMode.HTML)

    # ── Inline search ───────────────────────────────────────────────────────────

    @staticmethod
    def _inline_title_author_match(a_title: str, a_author: str,
                                   b_title: str, b_author: str) -> bool:
        """Fast title+author matching for inline merge/dedup.

        Returns True when both title and author are plausibly the same book.
        Uses substring containment + lowered/trimmed comparison.
        """
        at = a_title.lower().strip()
        bt = b_title.lower().strip()
        if not at or not bt:
            return False
        title_ok = at in bt or bt in at
        if not title_ok:
            return False
        aa = a_author.lower().strip()
        ba = b_author.lower().strip()
        if not aa or not ba:
            return True  # can't disqualify without author data
        return aa in ba or ba in aa

    @staticmethod
    def _inline_deterministic_id(title: str, author: str, source: str,
                                 isbn: str = "", idx: int = 0) -> str:
        """Generate a deterministic, process-stable inline result ID.

        Uses SHA-1 to avoid Python's randomized hash().
        Prefers ISBN when available.
        """
        if isbn:
            return f"isbn_{isbn}"
        raw = f"{title.lower().strip()}|{author.lower().strip()}|{source}|{idx}"
        h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
        return f"bk_{h}"

    @staticmethod
    def _dedup_tokens(value: str) -> list[str]:
        folded = unicodedata.normalize("NFKD", value or "")
        normalized = "".join(
            char for char in folded
            if not unicodedata.combining(char)
        ).casefold()
        # Keep letters from non-Latin scripts too; only punctuation and symbols
        # separate words. This avoids making every non-English title empty.
        tokens: list[str] = []
        current: list[str] = []
        for char in normalized:
            if char.isalnum():
                current.append(char)
            elif current:
                tokens.append("".join(current))
                current = []
        if current:
            tokens.append("".join(current))
        return tokens

    @classmethod
    def _author_tokens(cls, value: str) -> list[str]:
        """Normalize common duplicated-token noise without changing title tokens."""
        tokens = cls._dedup_tokens(value)
        collapsed: list[str] = []
        for token in tokens:
            if not collapsed or token != collapsed[-1]:
                collapsed.append(token)
        return collapsed

    @classmethod
    def _is_unknown_author(cls, value: str) -> bool:
        tokens = cls._author_tokens(value)
        return not tokens or " ".join(tokens) in {
            "unknown", "unknown author", "author unknown", "not known",
            "unspecified", "unspecified author", "n a", "na", "none",
        }

    @staticmethod
    def _one_edit_apart(left: str, right: str) -> bool:
        """Match a single typo/transposition in a sufficiently long surname."""
        if left == right:
            return True
        if min(len(left), len(right)) < 7 or abs(len(left) - len(right)) > 1:
            return False
        if len(left) == len(right):
            differences = [i for i, (a, b) in enumerate(zip(left, right)) if a != b]
            if len(differences) == 1:
                return True
            return (
                len(differences) == 2
                and differences[1] == differences[0] + 1
                and left[differences[0]] == right[differences[1]]
                and left[differences[1]] == right[differences[0]]
            )
        shorter, longer = (left, right) if len(left) < len(right) else (right, left)
        i = j = edits = 0
        while i < len(shorter) and j < len(longer):
            if shorter[i] == longer[j]:
                i += 1
                j += 1
            else:
                edits += 1
                j += 1
                if edits > 1:
                    return False
        return True

    @classmethod
    def _same_author_for_dedup(cls, left: str, right: str) -> bool:
        a = cls._author_tokens(left)
        b = cls._author_tokens(right)
        if cls._is_unknown_author(left) or cls._is_unknown_author(right):
            return False

        if sorted(a) == sorted(b):
            # Handles providers that return Eastern names in opposite orders.
            return True

        def names_match(a_names: list[str], b_names: list[str]) -> bool:
            if a_names == b_names:
                return True
            if not a_names or not b_names:
                return False
            if all(len(token) == 1 for token in a_names):
                return len(a_names) <= len(b_names) and all(
                    initial == full[0] for initial, full in zip(a_names, b_names)
                )
            if all(len(token) == 1 for token in b_names):
                return len(b_names) <= len(a_names) and all(
                    initial == full[0] for initial, full in zip(b_names, a_names)
                )
            return False

        # Try both conventional and reversed name order. A spelling-tolerant
        # surname comparison is accepted only with independent given-name data.
        for surname_a, given_a in ((a[-1], a[:-1]), (a[0], a[1:])):
            for surname_b, given_b in ((b[-1], b[:-1]), (b[0], b[1:])):
                if not names_match(given_a, given_b):
                    continue
                if surname_a == surname_b:
                    return True
                if given_a and given_b and cls._one_edit_apart(surname_a, surname_b):
                    return True
        return False

    @classmethod
    def _isbn_key(cls, book: dict) -> str:
        raw = next((book.get(key) for key in ("isbn", "isbn13", "isbn_13", "isbn10", "isbn_10")
                    if book.get(key)), "")
        value = "".join(char for char in str(raw).upper() if char.isalnum())
        if value.startswith("ISBN"):
            value = value[4:]
        if len(value) == 10 and value[:9].isdigit():
            stem = "978" + value[:9]
            weighted_sum = sum(
                int(digit) * (1 if index % 2 == 0 else 3)
                for index, digit in enumerate(stem)
            )
            checksum = (10 - weighted_sum % 10) % 10
            value = stem + str(checksum)
        return value

    @classmethod
    def _normalized_work_title(cls, book: dict) -> str:
        raw_title = (book.get("title", "") or "").strip()
        author = book.get("author", "") or ""
        if not raw_title:
            return ""

        # Remove author credits attached to a title by some metadata providers,
        # but only when the credit matches the record's separate author field.
        parts = re.split(r"\s+(?:-|–|—|:|\|)\s+", raw_title, maxsplit=1)
        if len(parts) == 2:
            left, right = parts
            if cls._same_author_for_dedup(left, author):
                raw_title = right
            elif cls._same_author_for_dedup(right, author):
                raw_title = left

        # Catalogs alternate between "&" and "and" in otherwise identical
        # titles (for example, "Angels & Demons" / "Angels and Demons").
        # Canonicalize the symbol only for work identity; display titles remain
        # untouched and edition/subtitle text is still retained.
        tokens = cls._dedup_tokens(raw_title.replace("&", " and "))
        author_tokens = cls._author_tokens(author)
        # Remove "by Author" only when the suffix matches the author field.
        for index, token in enumerate(tokens):
            if token == "by" and index > 0:
                suffix_author = " ".join(tokens[index + 1:])
                if (cls._same_author_for_dedup(suffix_author, author)
                        or (cls._is_unknown_author(suffix_author)
                            and cls._is_unknown_author(author))):
                    tokens = tokens[:index]
                    break

        # Broken catalog strings sometimes leave the generic tail "Novel by".
        # Strip only this incomplete attribution tail; meaningful subtitles and
        # edition labels remain part of the title key.
        if len(tokens) >= 3 and tokens[-2:] == ["novel", "by"]:
            tokens = tokens[:-2]
        # Some catalogs include a leading English article while others omit it
        # (e.g. "The Metamorphosis" vs "Metamorphosis"). Treat that as the same
        # work for deduplication without rewriting the displayed title.
        if len(tokens) > 1 and tokens[0] in {"a", "an", "the"}:
            tokens = tokens[1:]
        return " ".join(tokens)

    @staticmethod
    def _is_useful_dedup_value(value) -> bool:
        if value is None or value == "" or value == "N/A":
            return False
        if isinstance(value, (int, float)):
            return value > 0
        return True

    @staticmethod
    def _dedup_number(value) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _dedup_record_completeness(
        cls, book: dict, preferred_language: str | None = None
    ) -> int:
        """Score a duplicate record for choosing the metadata/cover seed.

        Cover URLs belong to a provider's edition record, so choose a complete
        record as a unit instead of taking its cover from one duplicate while
        assembling the displayed metadata from another. Ties intentionally keep
        the earlier provider result for stable behavior.
        """
        score = 0
        if book.get("cover_url"):
            score += 4
        if book.get("cover_source") == "google_books" and book.get("gb_volume_id"):
            score += 3
        if cls._isbn_key(book):
            score += 3
        if book.get("page_count"):
            score += 1
        if book.get("published_date"):
            score += 1
        if book.get("info_link"):
            score += 1
        description = str(book.get("description") or "").strip()
        if description:
            score += 1
            if len(description) >= 120:
                score += 1
        language = str(book.get("language") or "").casefold()
        preferred_prefix = (preferred_language or "").casefold()
        if preferred_prefix and language.startswith(preferred_prefix):
            score += 20
        return score

    def _merge_duplicate_book_data(
        self, target: dict, other: dict, preferred_language: str | None = None
    ) -> None:
        """Merge duplicates while preferring the more informative record."""
        target_language = str(target.get("language") or "").casefold()
        other_language = str(other.get("language") or "").casefold()
        preferred_prefix = (preferred_language or "").casefold()
        prefer_other_edition = bool(
            preferred_prefix
            and other_language.startswith(preferred_prefix)
            and not target_language.startswith(preferred_prefix)
        )
        target_is_preferred_edition = bool(
            preferred_prefix and target_language.startswith(preferred_prefix)
        )
        if prefer_other_edition:
            # Keep metadata from the preferred-language edition together, so
            # translated descriptions don't replace an English edition's data.
            for key in (
                "title", "description", "isbn", "page_count", "published_date",
                "categories", "language", "info_link", "gb_volume_id", "source",
                "cover_url", "cover_source",
            ):
                if self._is_useful_dedup_value(other.get(key)):
                    target[key] = other[key]

        target_title = target.get("title", "") or ""
        other_title = other.get("title", "") or ""
        if (not prefer_other_edition
                and len(self._dedup_tokens(other_title)) < len(self._dedup_tokens(target_title))):
            target["title"] = other_title
        target_author = target.get("author", "") or ""
        other_author = other.get("author", "") or ""
        if self._is_unknown_author(target_author) and not self._is_unknown_author(other_author):
            target["author"] = other_author
        elif not self._is_unknown_author(target_author) and not self._is_unknown_author(other_author):
            if len(" ".join(self._author_tokens(other_author))) > len(" ".join(self._author_tokens(target_author))):
                target["author"] = other_author

        for key, value in other.items():
            if key in {"title", "author", "rating", "rating_count", "rating_formatted",
                       "search_rating", "search_rating_count",
                       "search_rating_formatted"}:
                continue
            current = target.get(key)
            if not self._is_useful_dedup_value(current) and self._is_useful_dedup_value(value):
                target[key] = value
            elif (key == "description" and value and current
                  and not (target_is_preferred_edition
                           and not other_language.startswith(preferred_prefix))):
                if len(str(value)) > len(str(current)):
                    target[key] = value
            elif key == "categories" and value:
                target[key] = list(dict.fromkeys((current or []) + value))

        for prefix in ("search_", ""):
            rating_key = f"{prefix}rating"
            count_key = f"{prefix}rating_count"
            formatted_key = f"{prefix}rating_formatted"
            current_rating = self._dedup_number(target.get(rating_key))
            other_rating = self._dedup_number(other.get(rating_key))
            current_count = self._dedup_number(target.get(count_key))
            other_count = self._dedup_number(other.get(count_key))
            if other_rating > 0 and (
                current_rating <= 0
                or other_count > current_count
                or (other_count == current_count and other_rating > current_rating)
            ):
                for key in (rating_key, count_key, formatted_key):
                    if other.get(key) is not None:
                        target[key] = other[key]

    def _deduplicate_search_results(
        self, books: list[dict], query: str,
        preferred_language: str | None = None,
    ) -> list[dict]:
        """Merge duplicate work records using ISBN, normalized title and author evidence.

        Edition/subtitle text is retained in the title key. Unknown-author records
        join a known-author cluster only when that title has a single unambiguous
        author cluster in the result set.
        """
        records = [dict(book) for book in (books or [])]
        count = len(records)
        logger.info("DEBUG DEDUP in=%d", count)
        logger.info("DEBUG DEDUP in=%d", count)
        parents = list(range(count))

        def find(index: int) -> int:
            while parents[index] != index:
                parents[index] = parents[parents[index]]
                index = parents[index]
            return index

        def union(left: int, right: int) -> None:
            root_left, root_right = find(left), find(right)
            if root_left != root_right:
                parents[root_right] = root_left

        title_keys = [self._normalized_work_title(book) for book in records]
        isbn_keys = [self._isbn_key(book) for book in records]

        # First merge only strong identity matches, independent of provider order.
        for left in range(count):
            for right in range(left + 1, count):
                if isbn_keys[left] and isbn_keys[left] == isbn_keys[right]:
                    union(left, right)
                    continue
                if not title_keys[left] or title_keys[left] != title_keys[right]:
                    continue
                author_left = records[left].get("author", "") or ""
                author_right = records[right].get("author", "") or ""
                if (not self._is_unknown_author(author_left)
                        and not self._is_unknown_author(author_right)
                        and self._same_author_for_dedup(author_left, author_right)):
                    union(left, right)
                elif (self._is_unknown_author(author_left)
                      and self._is_unknown_author(author_right)):
                    union(left, right)

        # Attach unknown-author copies only if the normalized title has exactly
        # one known author cluster; if authors conflict, preserve ambiguity.
        title_groups: dict[str, list[int]] = {}
        for index, title_key in enumerate(title_keys):
            if title_key:
                title_groups.setdefault(title_key, []).append(index)
        for indices in title_groups.values():
            known_roots: set[int] = set()
            unknown_roots: set[int] = set()
            for index in indices:
                root = find(index)
                if self._is_unknown_author(records[index].get("author", "")):
                    unknown_roots.add(root)
                else:
                    known_roots.add(root)
            if len(known_roots) == 1 and unknown_roots:
                known_root = next(iter(known_roots))
                for unknown_root in unknown_roots:
                    union(known_root, unknown_root)

        record_clusters: dict[int, list[tuple[int, dict]]] = {}
        for index, book in enumerate(records):
            root = find(index)
            record_clusters.setdefault(root, []).append((index, book))

        clusters: dict[int, dict] = {}
        for root, entries in record_clusters.items():
            # Keep cover and edition metadata together. If equally complete,
            # max() keeps the first record because entries preserve provider order.
            seed_index, seed_book = max(
                entries,
                key=lambda entry: self._dedup_record_completeness(
                    entry[1], preferred_language=preferred_language
                ),
            )
            clusters[root] = dict(seed_book)
            for index, book in entries:
                if index == seed_index:
                    continue
                self._merge_duplicate_book_data(
                    clusters[root], book, preferred_language=preferred_language
                )

        merged = list(clusters.values())
        removed = count - len(merged)
        if removed:
            logger.info("Removed %s duplicate title/author results for query=%r", removed, query)
        return merged

    def _merge_inline_search_results(
        self, hardcover_books: list[dict], itunes_books: list[dict], query: str
    ) -> list[dict]:
        """Merge inline providers and collapse duplicate works across both lists."""
        # Hardcover first gives its records precedence when the deduplicator has
        # equally complete candidates; iTunes can still fill missing metadata.
        return self._deduplicate_search_results(
            [*(hardcover_books or []), *(itunes_books or [])], query,
        )

    def _rank_search_results(
        self, books: list[dict], query: str,
        preferred_language: str | None = None,
    ) -> list[dict]:
        """Stable relevance ordering using query token coverage in title and author."""
        query_tokens = [token for token in self._dedup_tokens(query)
                        if token not in self._STOPWORDS]
        logger.info("DEBUG INSIDE RANK query_tokens=%s books_in=%d", query_tokens, len(books or []))
        if not query_tokens or len(books or []) < 2:
            return list(books or [])

        def score(book: dict) -> float:
            all_title_tokens = self._dedup_tokens(book.get("title", ""))
            title_tokens = {
                token for token in all_title_tokens if token not in self._STOPWORDS
            }
            author_tokens = set(self._dedup_tokens(book.get("author", "")))
            matched = sum(1 for token in query_tokens
                          if token in title_tokens or token in author_tokens)
            title_coverage = sum(1 for token in query_tokens if token in title_tokens)
            title_precision = title_coverage / max(1, len(title_tokens))
            exact_title = all_title_tokens == self._dedup_tokens(query)
            return (
                (matched / len(query_tokens)) * 2
                + (title_coverage / len(query_tokens))
                + title_precision * 0.5
                + (0.5 if exact_title else 0.0)
            )

        if preferred_language:
            language_prefix = preferred_language.casefold()
            return sorted(
                books,
                key=lambda book: (
                    str(book.get("language") or "").casefold().startswith(language_prefix),
                    score(book),
                ),
                reverse=True,
            )
        return sorted(books, key=score, reverse=True)

    def _filter_clarification_like_results(
        self, books: list[dict], query: str,
        title_hint: str, author_hint: str,
    ) -> list[dict]:
        """Keep query-relevant records if discovery could not verify a pair."""
        query_tokens = [
            token for token in self._dedup_tokens(query)
            if token not in self._STOPWORDS
        ]
        title_tokens = [
            token for token in self._dedup_tokens(title_hint)
            if token not in self._STOPWORDS
        ]
        relevant = []
        for book in books:
            record_tokens = set(self._dedup_tokens(
                f"{book.get('title') or ''} {book.get('author') or ''}"
            ))
            if query_tokens and all(token in record_tokens for token in query_tokens):
                relevant.append(book)
                continue
            # Romanized author queries can match an edition whose author is
            # stored in another script; retain it when the title clue matches.
            author = str(book.get("author") or "")
            try:
                romanized_hint = author_hint.encode("ascii").decode("ascii")
            except (UnicodeDecodeError, UnicodeEncodeError):
                romanized_hint = ""
            if (romanized_hint and any(ord(char) > 127 for char in author)
                    and title_tokens and all(
                        token in set(self._dedup_tokens(book.get("title") or ""))
                        for token in title_tokens
                    )):
                relevant.append(book)
        return relevant

    async def inline_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline queries with debounce.

        - Queries < 3 chars are ignored (no API call).
        - A ~800 ms debounce prevents searching on every keystroke.
        - New queries from the same user cancel any pending search.
        - Only the latest query may answer Telegram.
        - Results use InlineQueryResultArticle with compact article style and callback buttons.
        """
        query = (update.inline_query.query or "").strip()
        user_id = update.inline_query.from_user.id
        query_id = update.inline_query.id

        # Inline Mini App launch shortcuts return Telegram's native launch
        # button above the results list, without inserting a filler message.
        launch_pages = {
            ".portal": ("", "📚 Open Annie Search Portal"),
            ".recom": ("recommendations", "✨ Open Annie Recommendations"),
            ".bookshelf": ("bookshelf", "📚 Open My Bookshelf"),
            ".favorites": ("favorites", "♥ Open Favourites"),
        }
        normalized_inline_query = query.casefold()
        launch = launch_pages.get(normalized_inline_query)
        if normalized_inline_query and not launch and any(
            shortcut.startswith(normalized_inline_query)
            for shortcut in launch_pages
        ):
            # Telegram sends every partial query as the user types. Don't run
            # book searches for `.por`, `.re`, etc. while a launch shortcut is
            # still being entered.
            await update.inline_query.answer([], cache_time=0, is_personal=True)
            return
        if launch:
            page, label = launch
            url = self._mini_app_url(page)
            if not url:
                await update.inline_query.answer([], cache_time=0, is_personal=True)
                return
            # Signed ticket validation is stateless, so the API can be handled
            # by any Koyeb replica without redirecting the user to private chat.
            ticket = self._issue_inline_app_ticket(update.inline_query.from_user)
            url = self._mini_app_url(page, inline_ticket=ticket)
            button = InlineQueryResultsButton(
                text=label, web_app=WebAppInfo(url=url)
            )
            await update.inline_query.answer(
                [], cache_time=0, is_personal=True, button=button
            )
            logger.info("Inline Mini App launch requested: page=%s user=%s", page or "portal", user_id)
            return

        # Launch shortcuts remain available even if a user is cooling down from
        # book searches. Apply search restrictions only to actual catalog queries.
        if self._active_clarification_restriction(user_id) is not None:
            await self._load_bot_admin_ids()
        if self._active_clarification_restriction(user_id) is not None:
            await update.inline_query.answer([], cache_time=1, is_personal=True)
            return

        # ── 1. Short queries: return empty immediately ────────────────────────────
        if len(query) < 3:
            logger.debug(f"Inline query too short: '{query}' user={user_id}")
            await update.inline_query.answer([], cache_time=60, is_personal=False)
            return

        logger.info(f"Inline query received: '{query}' user={user_id}")

        # ── 2. Cancel any previous pending search for this user ───────────────────
        async with self._inline_debounce_lock:
            prev = self._inline_debounce_tasks.get(user_id)
            if prev and not prev.done():
                prev.cancel()
                logger.info(f"Inline debounce cancelled: user={user_id}")
            # Start a new debounced search task
            task = asyncio.create_task(
                self._debounced_search(query, user_id, query_id)
            )
            self._inline_debounce_tasks[user_id] = task
            task.add_done_callback(
                lambda t: self._cleanup_debounce_task(user_id, t)
            )

    def _cleanup_debounce_task(self, user_id: int, task: asyncio.Task) -> None:
        """Remove a completed/cancelled task from the debounce dict."""
        try:
            task.result()
        except asyncio.CancelledError:
            pass  # expected for cancelled tasks
        except Exception:
            pass
        # Clean up if this task is still the one tracked for this user
        current = self._inline_debounce_tasks.get(user_id)
        if current is task:
            del self._inline_debounce_tasks[user_id]

    async def _debounced_search(self, query: str, user_id: int, query_id: str) -> None:
        """Wait ~800 ms then perform the Hardcover search. Cancelled if superseded."""
        DEBOUNCE_MS = 0.85  # seconds
        try:
            await asyncio.sleep(DEBOUNCE_MS)
        except asyncio.CancelledError:
            logger.info(f"Inline debounce cancelled: '{query}' user={user_id}")
            raise  # propagate so _cleanup_debounce_task handles it

        logger.info(f"Inline debounce completed: '{query}' user={user_id}")
        await self._do_inline_search(query, user_id, query_id)

    async def _do_inline_search(
        self, query: str, user_id: int, query_id: str
    ) -> None:
        """Perform the actual Hardcover search and send answerInlineQuery.

        Only sends if this is still the latest query for the user (not stale).
        """
        # Verify this is still the current pending query
        current_task = self._inline_debounce_tasks.get(user_id)
        if current_task is None or current_task.done():
            logger.debug(f"Inline search skipped (stale): '{query}'")
            return

        t_start = time.monotonic()
        logger.info(f"Inline search: '{query}'")

        from src.aggregator import MultiSourceBookAggregator as MSA

        # ── Phase 1: Concurrent Hardcover + iTunes ──────────────────────────────
        hc_task = asyncio.create_task(
            asyncio.to_thread(MSA.search_hardcover, query, 10)
        )
        itunes_task = asyncio.create_task(
            asyncio.to_thread(MSA.search_itunes, query)
        )

        done, _pending = await asyncio.wait(
            {hc_task, itunes_task},
            return_when=asyncio.ALL_COMPLETED,
        )

        # Collect books by source
        hc_books: list[dict] = []
        it_books: list[dict] = []
        for t in done:
            try:
                r = t.result()
                if not isinstance(r, list):
                    continue
                if r and r[0].get("source") == "hardcover":
                    hc_books.extend(r)
                else:
                    it_books.extend(r)
            except Exception:
                pass

        elapsed_fetch = time.monotonic() - t_start
        logger.info(
            f"⏱️ Inline sources fetched in {elapsed_fetch:.1f}s "
            f"(Hardcover={len(hc_books)}, iTunes={len(it_books)})"
        )

        # ── Phase 2: Merge — Hardcover primary, iTunes cover supplementation ──────
        merged: list[dict] = []
        matched_it_indices: set[int] = set()

        for hc in hc_books:
            book = hc.copy()
            for it_idx, it in enumerate(it_books):
                if it_idx in matched_it_indices:
                    continue
                if self._inline_title_author_match(
                    hc.get("title", ""), hc.get("author", ""),
                    it.get("title", ""), it.get("author", ""),
                ):
                    # Prefer iTunes cover when Hardcover lacks one
                    if it.get("cover_url") and not book.get("cover_url"):
                        book["cover_url"] = it["cover_url"]
                    matched_it_indices.add(it_idx)
                    break
            merged.append(book)

        # Unmatched iTunes-only results
        for it_idx, it in enumerate(it_books):
            if it_idx not in matched_it_indices:
                merged.append(it.copy())

        # Keep inline results consistent with /search and the Mini App. The
        # Hardcover and iTunes catalogs often repeat an exact work under
        # different edition IDs; merge those records before Telegram renders
        # duplicate cards. This uses ISBN/title/author evidence, never cover art.
        has_latin_query = any(ch.isascii() and ch.isalpha() for ch in query)
        has_non_latin_query = any(ch.isalpha() and not ch.isascii() for ch in query)
        preferred_language = "en" if has_latin_query and not has_non_latin_query else None
        final = self._deduplicate_search_results(
            merged, query, preferred_language=preferred_language
        )[:20]
        elapsed = time.monotonic() - t_start
        logger.info(f"⏱️ Inline total: {elapsed:.1f}s → {len(final)} results")

        # ── Phase 3: Build InlineQueryResultArticle list ─────────────────────────
        results = []
        for idx, book in enumerate(final):
            title = book.get("title", "Unknown")
            author = book.get("author", "Unknown") or "Unknown"
            isbn = (book.get("isbn") or "").replace("-", "").strip()
            cover_url = book.get("cover_url") or ""
            source = book.get("source", "unknown")

            # Skip if no cover URL (we show thumbnails; fallback to None is allowed but discouraged)
            if not cover_url:
                logger.debug(f"Skipping book '{title}' due to missing cover URL")
                continue

            result_id = self._inline_deterministic_id(
                title, author, source, isbn=isbn, idx=idx,
            )

            # Compact description: author + rating + year
            desc_parts = [author]
            year = (book.get("published_date") or "")[:4]
            rating = book.get("rating_formatted") or book.get("rating")
            rating_cnt = book.get("rating_count", 0)
            if rating and rating_cnt:
                try:
                    stars = "⭐" * min(int(float(str(rating).replace(",", "."))), 5)
                    desc_parts.append(f"{stars} {rating}/5 · {rating_cnt:,} ratings")
                except ValueError:
                    pass
            if year:
                desc_parts.append(year)
            description = " • ".join(desc_parts)

            # Store book data in callback cache for later retrieval
            callback_key = f"inline_{user_id}_{result_id}"
            self._set_inline_callback_data(callback_key, book)

            # Skip if no title (required for InlineQueryResultArticle)
            if not title or title == "Unknown":
                logger.debug(f"Skipping book due to missing title")
                continue

            result_id = self._inline_deterministic_id(
                title, author, source, isbn=isbn, idx=idx,
            )

            # Store book data in callback cache for later retrieval
            callback_key = f"inline_{user_id}_{result_id}"
            self._set_inline_callback_data(callback_key, book)

            # Build message content for when result is selected
            message_content = InputTextMessageContent(
                message_text=self._build_inline_photo_caption(book),
                parse_mode=ParseMode.HTML,
            )

            # InlineQueryResultArticle — compact list/article style for inline results
            result = InlineQueryResultArticle(
                id=result_id,
                title=title,
                description=description,
                thumbnail_url=cover_url if cover_url else None,
                input_message_content=message_content,
                reply_markup=self._build_inline_photo_keyboard(callback_key),
            )

            results.append(result)

        logger.info(f"Inline result types: {', '.join('article' for _ in results)}")

        # ── Phase 4: Send answerInlineQuery ──────────────────────────────────────
        # Check once more that this is still the active query for the user
        current_task = self._inline_debounce_tasks.get(user_id)
        if current_task is None or current_task.done():
            logger.debug(f"Inline answer skipped (stale): '{query}'")
            return

        api_url = f"https://api.telegram.org/bot{self.token}/answerInlineQuery"
        payload = {
            "inline_query_id": query_id,
            "results": [result.to_dict() for result in results],
            "cache_time": 60,
            "is_personal": False,
            "next_offset": "",
        }
        try:
            resp = get_http_session().post(api_url, json=payload, timeout=5)
            if resp.status_code != 200 or not resp.json().get("ok"):
                logger.warning(f"answerInlineQuery failed: {resp.text}")
            else:
                logger.info(f"Inline answer sent: '{query}' → {len(results)} results")
        except Exception as e:
            logger.warning(f"answerInlineQuery error: {e}")

    # ── Inline photo helpers ────────────────────────────────────────────────────

    @staticmethod
    def _unique_book_categories(categories) -> list[str]:
        """Normalize provider genre labels and remove casing/spacing duplicates."""
        if isinstance(categories, str):
            categories = [categories]
        if not isinstance(categories, (list, tuple)):
            return []
        unique = []
        seen = set()
        for category in categories:
            if isinstance(category, dict):
                category = category.get("name") or category.get("title") or ""
            if not isinstance(category, str):
                continue
            category = " ".join(category.split())
            key = category.casefold()
            if category and key not in seen:
                seen.add(key)
                unique.append(category)
        return unique

    def _build_inline_photo_caption(self, book: dict) -> str:
        """Build compact caption for inline photo results (initial selection)."""
        title = html_escape(book.get("title", "Unknown"))
        author = html_escape(book.get("author", "Unknown"))
        isbn = html_escape(book.get("isbn", ""))
        pages = book.get("page_count", 0)
        year = (book.get("published_date") or "")[:4]
        rating = book.get("rating_formatted") or book.get("rating")
        rating_cnt = book.get("rating_count", 0)

        parts = [
            f"📖 Title: <b>{title}</b>",
            f"✍️ Author: {author}",
        ]

        categories = self._unique_book_categories(book.get("categories", []))
        if categories:
            genres_str = ", ".join(categories[:5])
            parts.append(f"🏷️ Genres: {html_escape(genres_str)}")

        if rating and rating_cnt:
            try:
                rating_num = float(str(rating).replace(",", "."))
                stars = "⭐" * min(int(rating_num), 5)
                parts.append(
                    f"📊 Rating: {stars} <b>{html_escape(str(rating))}</b>/5 "
                    f"(<b>{rating_cnt:,}</b> ratings)"
                )
            except ValueError:
                parts.append(
                    f"📊 Rating: <b>{html_escape(str(rating))}</b>/5 "
                    f"(<b>{rating_cnt:,}</b> ratings)"
                )

        if isbn:
            parts.append(f"📚 ISBN: <b>{isbn}</b>")
        if pages:
            parts.append(f"📄 Pages: <b>{pages}</b>")
        if year:
            parts.append(f"📅 Year: <b>{year}</b>")

        desc_text = (book.get("description") or "").strip()
        if desc_text:
            desc_text = re.sub(r"<[^>]+>", "", desc_text)
            desc_text = re.sub(r"\s+", " ", desc_text).strip()
            if len(desc_text) > 100:
                desc_text = desc_text[:97] + "..."
            desc_text = html_escape(desc_text)
            parts.append("")
            parts.append("📄 Summary")
            parts.append(f"&gt; {desc_text}")

        source = book.get("source", "unknown").replace("_", " ").title()
        parts.append("")
        parts.append(f"🔵 Source: {source}")

        return "\n".join(parts)

    def _build_inline_photo_keyboard(self, callback_key: str) -> InlineKeyboardMarkup:
        """Build the compact inline-result keyboard."""
        keyboard = [[InlineKeyboardButton(
            "View More", callback_data=f"inline_more_{callback_key}"
        )]]
        return InlineKeyboardMarkup(keyboard)

    def _build_expanded_inline_caption(self, book: dict) -> str:
        """Build expanded caption for when hourglass button is pressed."""
        title = html_escape(book.get("title", "Unknown"))
        author = html_escape(book.get("author", "Unknown"))
        isbn = html_escape(book.get("isbn", ""))
        pages = book.get("page_count", 0)
        year = (book.get("published_date") or "")[:4]
        lang = book.get("language", "")
        publisher = html_escape(book.get("publisher", ""))

        parts = [
            f"📖 <b>Title:</b> {title}",
            f"✍️ <b>Author:</b> {author}",
        ]

        # Genres
        categories = self._unique_book_categories(book.get("categories", []))
        if categories:
            genres_str = ", ".join(categories)
            parts.append(f"🏷️ <b>Genres:</b> {html_escape(genres_str)}")

        # Rating
        rating = book.get("rating_formatted") or book.get("rating")
        rating_cnt = book.get("rating_count", 0)
        rating_reviews = book.get("rating_reviews", 0)
        if rating and rating_cnt:
            try:
                rating_num = float(str(rating).replace(",", "."))
                stars = "⭐" * min(int(rating_num), 5)
                parts.append(
                    f"📊 <b>Rating:</b> {stars} <b>{html_escape(str(rating))}</b>/5 "
                    f"(<b>{rating_cnt:,}</b> ratings"
                    f"{f', {rating_reviews:,} reviews' if rating_reviews else ''})"
                )
            except ValueError:
                parts.append(
                    f"📊 <b>Rating:</b> {html_escape(str(rating))}/5 "
                    f"(<b>{rating_cnt:,}</b> ratings"
                    f"{f', {rating_reviews:,} reviews' if rating_reviews else ''})"
                )

        if year:
            parts.append(f"📅 <b>Published:</b> {year}")
        if lang:
            parts.append(f"🌐 <b>Language:</b> {html_escape(lang)}")
        if publisher:
            parts.append(f"🏢 <b>Publisher:</b> {publisher}")
        if pages:
            parts.append(f"📚 <b>Format:</b> {pages} pages")
        if isbn:
            parts.append(f"🆔 <b>ISBN:</b> <code>{isbn}</code>")

        # ASIN if available
        asin = book.get("asin", "")
        if asin:
            parts[-1] = parts[-1].replace("</code>", f" | <b>ASIN:</b> {html_escape(asin)}</code>")

        parts.append("")  # blank line

        # Description with expandable blockquote
        desc_text = (book.get("description") or "").strip()
        if desc_text:
            # Clean HTML tags
            desc_text = re.sub(r"<[^>]+>", "", desc_text)
            desc_text = re.sub(r"\s+", " ", desc_text).strip()
            # Escape for HTML
            desc_text = html_escape(desc_text)
            # Truncate plain text to safe length BEFORE wrapping in HTML
            max_desc_length = 800  # Conservative limit for description
            if len(desc_text) > max_desc_length:
                # Truncate at word boundary
                truncated = desc_text[:max_desc_length]
                last_space = truncated.rfind(" ")
                if last_space > max_desc_length * 0.8:
                    desc_text = truncated[:last_space] + "..."
                else:
                    desc_text = truncated + "..."
            parts.append("")
            parts.append("📄 <b>Summary</b>")
            parts.append(f"<blockquote expandable>{desc_text}</blockquote>")

        parts.append("")
        parts.append("🔵 <b>Source:</b> {source}".format(source=book.get("source", "unknown").replace('_', ' ').title()))

        # Join and ensure length is safe
        caption = "\n".join(parts)
        # Final safety check - if still too long, remove optional fields
        if len(caption) > 1020:
            # Remove ASIN line if present
            if asin:
                for i, part in enumerate(parts):
                    if "ASIN:" in part:
                        parts.pop(i)
                        break
                caption = "\n".join(parts)
            # If still too long, remove publisher
            if len(caption) > 1020 and publisher:
                for i, part in enumerate(parts):
                    if "Publisher:" in part:
                        parts.pop(i)
                        break
                caption = "\n".join(parts)
            # If still too long, remove language
            if len(caption) > 1020 and lang:
                for i, part in enumerate(parts):
                    if "Language:" in part:
                        parts.pop(i)
                        break
                caption = "\n".join(parts)
            # If still too long, remove published year
            if len(caption) > 1020 and year:
                for i, part in enumerate(parts):
                    if "Published:" in part:
                        parts.pop(i)
                        break
                caption = "\n".join(parts)
            # If still too long, remove pages
            if len(caption) > 1020 and pages:
                for i, part in enumerate(parts):
                    if "Format:" in part:
                        parts.pop(i)
                        break
                caption = "\n".join(parts)
            # Last resort: truncate description further
            if len(caption) > 1020:
                for i, part in enumerate(parts):
                    if part == "📄 <b>Summary</b>":
                        if i + 1 < len(parts) and parts[i + 1].startswith("<blockquote expandable>"):
                            current_desc = parts[i + 1][23:-13]
                            parts_without_desc = parts[:i+1] + [""] + parts[i+2:]
                            base_length = len("\n".join(parts_without_desc))
                            max_desc_len = 1020 - base_length - 3
                            if max_desc_len > 10:
                                if len(current_desc) > max_desc_len:
                                    truncated = current_desc[:max_desc_len]
                                    last_space = truncated.rfind(" ")
                                    if last_space > max_desc_len * 0.8:
                                        truncated = truncated[:last_space]
                                    parts[i + 1] = f"<blockquote expandable>{truncated}...</blockquote>"
                            break
                caption = "\n".join(parts)

        return caption

    def _build_goodreads_keyboard(self, book: dict) -> InlineKeyboardMarkup:
        """Build keyboard with Goodreads button for expanded view."""
        gr_url = build_goodreads_url(book)
        keyboard = [[InlineKeyboardButton("📚 Open Goodreads 🔗", url=gr_url)]]
        return InlineKeyboardMarkup(keyboard)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def download_and_save_image(self, cover_url: str, book: dict = None):
        """Download, validate, and normalize a cover to JPEG for Telegram.

        Some catalog URLs fail only for individual editions or return formats
        Telegram cannot send as photos. Retry other available catalogs after a
        failed/placeholder image, and always upload a decoded JPEG.
        """
        book = book or {}
        title = book.get("title", "")
        primary_source = book.get("cover_source", book.get("source", "unknown"))
        candidates: list[tuple[str, str]] = []

        def add_candidate(url: str | None, source: str) -> None:
            if url and url.startswith(("https://", "http://")) and all(url != old for old, _ in candidates):
                candidates.append((url, source))

        add_candidate(cover_url, primary_source)
        hc_match = book.get("_hardcover_match") or {}
        add_candidate(hc_match.get("cover_url"), "hardcover")

        initial_candidate_count = len(candidates)

        def try_candidates(candidate_list: list[tuple[str, str]]) -> str | None:
            for candidate_url, source in candidate_list:
                try:
                    logger.info("📥 Downloading cover: %s... source=%s title=%s", candidate_url[:60], source, title)
                    response = get_http_session().get(
                        candidate_url, headers=HEADERS, timeout=15, allow_redirects=True
                    )
                    response.raise_for_status()
                    content = response.content
                    if not content or is_placeholder_image(content):
                        logger.warning("Cover is empty or placeholder: source=%s title=%s", source, title)
                        continue

                    with Image.open(BytesIO(content)) as image:
                        image.load()
                        width, height = image.size
                        # Telegram expects a supported photo format. Converting
                        # WebP/AVIF/PNG responses avoids format-specific sendPhoto failures.
                        normalized = image.convert("RGB")
                        output = BytesIO()
                        normalized.save(output, format="JPEG", quality=92, optimize=True)
                        jpeg_content = output.getvalue()

                    logger.info(
                        "Cover validated: source=%s status=%s type=%s bytes=%s dims=%sx%s",
                        source,
                        response.status_code,
                        response.headers.get("Content-Type", ""),
                        len(content),
                        width,
                        height,
                    )
                    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
                    temp_file.write(jpeg_content)
                    temp_file.close()
                    if candidate_url != cover_url:
                        book["cover_url"] = candidate_url
                        book["cover_source"] = source
                    logger.info("✅ Downloaded and normalized cover: source=%s title=%s", source, title)
                    return temp_file.name
                except Exception as exc:
                    logger.warning("Cover candidate failed: source=%s title=%s error=%s", source, title, exc)
            return None

        # Try the supplied URL and any already-cached Hardcover image first.
        temp_path = try_candidates(candidates)
        if temp_path:
            return temp_path

        # Only make extra provider lookups when those images were absent,
        # inaccessible, invalid, or placeholders.
        fallback_index = len(candidates)
        if primary_source != "itunes":
            try:
                itunes_results = MultiSourceBookAggregator.search_itunes(
                    f"{title} {book.get('author', '')}".strip()
                )
                itunes_match = MultiSourceBookAggregator._find_matching_book_strict(
                    title, book.get("author", ""), itunes_results
                )
                if itunes_match:
                    add_candidate(itunes_match.get("cover_url"), "itunes")
            except Exception as exc:
                logger.debug("iTunes cover recovery lookup failed for %s: %s", title, exc)

        temp_path = try_candidates(candidates[fallback_index:])
        if temp_path:
            return temp_path

        if not hc_match.get("cover_url"):
            fallback_index = len(candidates)
            try:
                hc_cover = MultiSourceBookAggregator._get_hardcover_cached(
                    book.get("isbn", ""), title, book.get("author", "")
                )[3]
                add_candidate(hc_cover, "hardcover")
            except Exception as exc:
                logger.debug("Hardcover cover recovery lookup failed for %s: %s", title, exc)

            temp_path = try_candidates(candidates[fallback_index:])
            if temp_path:
                return temp_path

        fallback_index = len(candidates)
        isbn_cover = MultiSourceBookAggregator._get_openlibrary_cover(book.get("isbn", ""))
        add_candidate(isbn_cover, "open_library")

        temp_path = try_candidates(candidates[fallback_index:])
        if temp_path:
            return temp_path

        logger.warning("No usable cover found across providers for: %s", title)
        return None

    def cleanup_temp_file(self, file_path: str):
        """Delete a temporary file, silently ignoring errors."""
        try:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
        except Exception as e:
            logger.error(f"Error cleaning up: {e}")

    def format_book_message(self, book: dict) -> str:
        """Format book info for display using Telegram HTML."""
        title = html_escape(book.get("title", "Unknown"))
        translated_title = str(book.get("translated_title") or "").strip()
        author = html_escape(book.get("author", "Unknown"))
        rating = html_escape(str(book.get("rating_formatted", book.get("rating", "N/A"))))
        rating_cnt = book.get("rating_count", 0)
        isbn = html_escape(book.get("isbn", ""))
        pages = str(book.get("page_count", 0))
        year = book.get("published_date", "")[:4]
        desc = str(book.get("description") or "").strip()

        # Preserve paragraph boundaries from HTML and plain-text descriptions.
        desc = re.sub(r"<br\s*/?>", "\n", desc, flags=re.IGNORECASE)
        desc = re.sub(
            r"</(?:p|div|li|h[1-6]|blockquote)\s*>", "\n\n", desc,
            flags=re.IGNORECASE,
        )
        desc = re.sub(r"<[^>]+>", "", desc)
        desc = html_unescape(desc)
        desc = re.sub(r"[\t\f\v ]+", " ", desc)
        desc = re.sub(r" *\n *", "\n", desc)
        desc = re.sub(r"\n{3,}", "\n\n", desc).strip()
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", desc) if part.strip()]

        # Some providers return one unbroken paragraph. Split long prose at
        # sentence boundaries so it remains readable in Telegram captions.
        if len(paragraphs) == 1 and len(paragraphs[0]) > 360:
            sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"“'(])", paragraphs[0])
            if len(sentences) > 1:
                chunks = []
                current = ""
                for sentence in sentences:
                    candidate = f"{current} {sentence}".strip()
                    if current and len(candidate) > 300:
                        chunks.append(current)
                        current = sentence
                    else:
                        current = candidate
                if current:
                    chunks.append(current)
                paragraphs = chunks
        desc = "\n\n".join(paragraphs)

        # Source badge
        gr_enhanced = book.get("gr_enhanced", False)
        cover_source = book.get("cover_source", book.get("source", "unknown"))
        source_emoji = {
            "google_books": "🔵",
            "open_library": "🟢",
            "itunes": "🟠",
            "goodreads": "🔴",
        }.get(cover_source, "⚪")
        source_text = "Goodreads" if gr_enhanced else cover_source.replace("_", " ").title()

        header = []

        # Header
        header.append(f"📖 Title: <b>{title}</b>")
        if translated_title and translated_title.casefold() != str(book.get("title") or "").strip().casefold():
            header.append(f"🌐 <i>English title: {html_escape(translated_title)}</i>")
        header.append(f"✍️ Author: <i>{author}</i>")

        # Rating line with stars
        if rating_cnt > 0:
            try:
                rating_for_float = str(book.get("rating", "")).replace(",", ".")
                rating_num = float(rating_for_float)
                stars = "⭐" * min(int(rating_num), 5)
                header.append(f"⭐ Rating: {stars} <b>{rating}</b>/5 (<b>{rating_cnt:,} ratings</b>)")
            except ValueError:
                header.append(f"⭐ Rating: <b>{rating}</b>/5 (<b>{rating_cnt:,} ratings</b>)")
        else:
            header.append("⭐ Rating: ❓ <b>No ratings yet</b>")

        # Metadata
        metadata = []
        if isbn:
            metadata.append(f"🆔 ISBN: <code>{isbn}</code>")
        if pages and int(pages) > 0:
            metadata.append(f"📄 Pages: <code>{pages}</code>")
        if year:
            metadata.append(f"📅 Year: <code>{year}</code>")

        # Genres
        categories = self._unique_book_categories(book.get("categories", []))
        if categories:
            genres_str = ", ".join(categories[:5])
            metadata.append(f"🏷️ Genres: {html_escape(genres_str)}")

        # Footer
        footer = [f"{source_emoji} <i>Source: {html_escape(source_text)}</i>"]

        # Links
        if book.get("info_link"):
            footer.append(f'<a href="{html_escape(book["info_link"])}">📚 More Info</a>')
        if book.get("goodreads_url"):
            footer.append(f'<a href="{html_escape(book["goodreads_url"])}">Goodreads Page</a>')

        def compose(description: str) -> str:
            groups = ["\n".join(header)]
            if metadata:
                # Keep metadata compact so the caption budget stays available
                # for the book description.
                groups.append("\n".join(metadata))
            if description:
                groups.append(f"📄 <b>Summary</b>\n\n{html_escape(description)}")
            groups.append("\n".join(footer))
            return "\n\n".join(groups)

        caption = compose(desc)
        max_caption_length = 950  # Telegram allows 1024 caption characters.
        if len(caption) > max_caption_length and desc:
            # Budget against the fully rendered HTML so escaping and links are
            # counted too. Find the fitting prefix, then cut back to a complete
            # word so the visible description never ends mid-word.
            truncation_mark = "...."
            low, high = 0, len(desc)
            while low < high:
                middle = (low + high + 1) // 2
                if len(compose(desc[:middle].rstrip() + truncation_mark)) <= max_caption_length:
                    low = middle
                else:
                    high = middle - 1
            shortened = desc[:low].rstrip()
            if low < len(desc):
                boundaries = [shortened.rfind(char) for char in (" ", "\n", "\t")]
                boundary = max(boundaries)
                shortened = shortened[:boundary].rstrip() if boundary >= 0 else ""
                shortened += truncation_mark
            caption = compose(shortened)

        return caption

    @staticmethod
    async def _translate_book_text_fields(book: dict) -> dict:
        """Translate a selected book's title and description without blocking the bot loop."""
        title = str(book.get("title") or "").strip()
        description = str(book.get("description") or "").strip()
        tasks = []
        fields = []
        if title and not is_english_description(title):
            fields.append("title")
            tasks.append(asyncio.to_thread(translate_to_english, title))
        if description and not is_english_description(description):
            fields.append("description")
            tasks.append(asyncio.to_thread(translate_to_english, description))
        if tasks:
            translated_values = await asyncio.gather(*tasks, return_exceptions=True)
            for field, translated in zip(fields, translated_values):
                if isinstance(translated, Exception) or not translated:
                    continue
                original = title if field == "title" else description
                if str(translated).strip() != original:
                    if field == "title":
                        book["translated_title"] = str(translated).strip()
                    else:
                        book["description"] = str(translated).strip()
        return book

    # ── Search helpers ─────────────────────────────────────────────────────────
    async def _preload_hardcover_ratings_for_page(
        self, books: list, page_num: int, page_size: int
    ) -> None:
        """Preload Hardcover ratings for visible books on one page (concurrent).

        Uses asyncio.gather() with one asyncio.to_thread() task per visible
        book so the 5 lookups run genuinely in parallel. Hardcover is the
        SINGLE source of truth for list ratings. Uses the existing _hc_cache
        (no new cache layer).
        """
        start_idx = (page_num - 1) * page_size
        end_idx = min(page_num * page_size, len(books))
        visible = books[start_idx:end_idx]

        preload_started = time.perf_counter()
        logger.info(f"Normal search Hardcover preload started: page={page_num}, books={len(visible)}")

        tasks = [
            asyncio.to_thread(
                MultiSourceBookAggregator._get_hardcover_cached,
                book.get("isbn") or "",
                book.get("title") or "",
                book.get("author") or "",
            )
            for book in visible
        ]
        results = await asyncio.gather(*tasks)

        for book, (hc_rating, hc_count, hc_genres, hc_cover) in zip(visible, results):
            title = book.get("title") or ""
            author = book.get("author") or ""
            isbn = book.get("isbn") or ""

            if hc_rating > 0:
                book["search_rating"] = hc_rating
                book["search_rating_count"] = hc_count
                book["search_rating_formatted"] = f"{hc_rating:.2f}"
                book["_hardcover_match"] = {
                    "title": title,
                    "author": author,
                    "isbn": isbn,
                    "rating": hc_rating,
                    "rating_count": hc_count,
                    "categories": hc_genres,
                    "cover_url": hc_cover,
                }
                logger.info(
                    f"Normal search Hardcover rating: {title} -> {hc_rating:.2f} ({hc_count} ratings)"
                )
            else:
                logger.info(f"Normal search Hardcover rating unavailable: {title}")

        logger.info(
            "Normal search Hardcover preload completed: page=%s elapsed_ms=%d",
            page_num,
            round((time.perf_counter() - preload_started) * 1000),
        )

    def _build_search_results_message(
        self, books: list, query_text: str, user_id: int, page_num: int, page_size: int
    ) -> tuple[str, InlineKeyboardMarkup]:
        """Build formatted search results message with full titles, authors, ratings, and pagination."""
        total_pages = max(1, (len(books) + page_size - 1) // page_size)
        start_idx = (page_num - 1) * page_size
        end_idx = min(page_num * page_size, len(books))

        parts = [
            f"📚 <b>Search Results</b>",
            f"🔎 <i>{html_escape(query_text)}</i>",
            "",
        ]

        for i in range(start_idx, end_idx):
            book = books[i]
            parts.append(f"📖 <b>{i + 1}.</b> {html_escape(book['title'])}")
            parts.append(f"   ✍️ {html_escape(book['author'])}")

            # Show rating if available (list-only fields, no extra API calls)
            if book.get("search_rating") is not None and book.get("search_rating", 0) > 0:
                parts.append(
                    f"   ⭐ {book.get('search_rating_formatted', 'N/A')} ({book['search_rating_count']:,} ratings)"
                )
            else:
                parts.append("   ❓ No ratings yet")

            parts.append("")  # blank line between entries

        # Footer
        if total_pages > 1:
            parts.append(f"👇 <i>Select a book — Page {page_num} of {total_pages}</i>")
        else:
            parts.append("👇 <i>Select a book:</i>")

        # Keyboard: compact numbered buttons in rows of 5 + pagination nav
        keyboard = []
        row = []
        for i in range(start_idx, end_idx):
            row.append(InlineKeyboardButton(str(i + 1), callback_data=f"book_{user_id}_{i}_{page_num}"))
            if len(row) == 5:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)

        if total_pages > 1:
            nav_row = []
            if page_num > 1:
                nav_row.append(InlineKeyboardButton("◀️", callback_data=f"page_{user_id}_{page_num - 1}"))
            nav_row.append(InlineKeyboardButton(f"{page_num}/{total_pages}", callback_data="noop"))
            if page_num < total_pages:
                nav_row.append(InlineKeyboardButton("▶️", callback_data=f"page_{user_id}_{page_num + 1}"))
            keyboard.append(nav_row)

        return "\n".join(parts), InlineKeyboardMarkup(keyboard)

    @staticmethod
    def _build_detail_keyboard(user_id: int, book_idx: int, chat_type: str) -> InlineKeyboardMarkup:
        """Build detail actions, keeping cover downloads private and group output closable."""
        actions = []
        if chat_type == "private":
            actions.append([InlineKeyboardButton(
                "📥 Download Cover", callback_data=f"download_{user_id}_{book_idx}"
            )])
        else:
            actions.append([InlineKeyboardButton(
                "🔙 Back to Results", callback_data=f"back_{user_id}"
            )])
            actions.append([InlineKeyboardButton(
                "✖️ Close", callback_data=f"close_{user_id}"
            )])
            return InlineKeyboardMarkup(actions)
        actions.append([InlineKeyboardButton(
            "🔙 Back to Results", callback_data=f"back_{user_id}"
        )])
        return InlineKeyboardMarkup(actions)

    # ── Search ────────────────────────────────────────────────────────────────
    async def search_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /search command."""
        status_message = None
        session_key = None
        group_search_key = None
        try:
            if not context.args:
                await update.message.reply_text(
                    "Please provide a book title or author name.\n\n"
                    "Example: <code>/search Harry Potter</code>",
                    parse_mode=ParseMode.HTML,
                    reply_parameters=ReplyParameters(
                        message_id=update.message.message_id,
                        allow_sending_without_reply=True,
                    ),
                )
                return

            query_text = " ".join(context.args)

            if not query_text or len(query_text.strip()) < 2:
                await update.message.reply_text(
                    "Please provide a valid search query (at least 2 characters).\n\n"
                    "Example: <code>/search Harry Potter</code>",
                    parse_mode=ParseMode.HTML,
                    reply_parameters=ReplyParameters(
                        message_id=update.message.message_id,
                        allow_sending_without_reply=True,
                    ),
                )
                return

            logger.info(f"👤 User search: {query_text}")

            if await self._reject_search_during_clarification_restriction(update):
                return

            if await self._group_search_is_rate_limited(update):
                return

            user_id = update.effective_user.id
            chat_id = update.effective_chat.id
            session_key = (chat_id, user_id)
            if update.effective_chat.type in ("group", "supergroup"):
                group_search_key = session_key
                self._group_search_inflight.add(group_search_key)
            await update.message.chat.send_action("typing")
            status_message = await update.message.reply_text(
                f"🔎 Searching for <i>{html_escape(query_text)}</i>…",
                parse_mode=ParseMode.HTML,
                reply_parameters=ReplyParameters(
                    message_id=update.message.message_id,
                    allow_sending_without_reply=True,
                ),
            )
            self._active_result_messages[session_key] = {
                "message_id": status_message.message_id,
                "query": query_text,
                "page": 1,
            }

            # An explicit "Title by Author" query can be verified directly.
            # For ordinary multiword searches, first inspect actual catalog
            # results; otherwise a title like "Harry Potter" can be split into
            # the false pair "Harry by Potter" by the discovery fallback.
            normalized_query = query_text.casefold().strip()
            has_clarification_cooldown = (
                (user_id, normalized_query) in self._clarification_rate_limit
            )
            if " by " in normalized_query or has_clarification_cooldown:
                stop = await self._try_clarification(update, query_text)
                if stop:
                    self._clear_active_result_message(session_key, status_message.message_id)
                    try:
                        await status_message.delete()
                    except Exception:
                        pass
                    return

            # Keep matching and ranking synchronous with the original search flow.
            # Only the slow list-rating preload moves after the first result render.
            books = await self._aggregate_search_results(query_text, limit=10)
            books = self._rank_search_results(
                self._deduplicate_search_results(books, query_text), query_text
            )
            needs_pair_check, title_hint, author_hint = self._is_clarification_query(query_text)
            if needs_pair_check:
                candidate = self._candidate_from_search_books(
                    books, query_text, title_hint or "", author_hint or ""
                )
                if candidate:
                    stop = await self._try_clarification(update, query_text, candidate)
                    if stop:
                        self._clear_active_result_message(session_key, status_message.message_id)
                        try:
                            await status_message.delete()
                        except Exception:
                            pass
                        return

            if not books:
                logger.warning(f"No books found for: {query_text}")
                self._clear_active_result_message(session_key, status_message.message_id)
                await status_message.edit_text(
                    f"❌ <b>No books found</b> for '<b>{html_escape(query_text)}</b>'\n\n"
                    "<i>Try different keywords or check spelling.</i>",
                    parse_mode=ParseMode.HTML,
                )
                return

            self._set_cached_books(user_id, books)
            self._search_page_cache[user_id] = 1
            self._search_query_cache[user_id] = query_text

            results_text, keyboard = self._build_search_results_message(
                books, query_text, user_id, 1, 5
            )
            await status_message.edit_text(
                text=results_text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )
            self._cache_result_message(
                chat_id, status_message.message_id, user_id, books, query_text, 1
            )
            self._schedule_result_rating_refresh(
                books, query_text, user_id, 1, chat_id,
                status_message.message_id, context.bot,
            )

        except Exception as e:
            logger.error(f"Error in search_command: {e}", exc_info=True)
            if status_message is not None:
                if session_key is not None:
                    self._clear_active_result_message(session_key, status_message.message_id)
                try:
                    await status_message.edit_text(
                        "❌ An error occurred while searching.",
                        parse_mode=ParseMode.HTML,
                    )
                    return
                except Exception:
                    pass
            await update.message.reply_text(
                "❌ An error occurred while searching.",
                parse_mode=ParseMode.HTML,
                reply_parameters=ReplyParameters(
                    message_id=update.message.message_id,
                    allow_sending_without_reply=True,
                ),
            )
        finally:
            if group_search_key is not None:
                self._group_search_inflight.discard(group_search_key)

    # ── Button callbacks ────────────────────────────────────────────────────────

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle all inline button presses."""
        try:
            query = update.callback_query
            callback_data = query.data

            is_channel_tools_callback = (
                callback_data in {"start_misc", "help_misc", "misc_home", "misc_connections", "misc_list_connections", "misc_index"}
                or callback_data.startswith(("disconnect_channel:", "index_"))
            )
            if is_channel_tools_callback and not await self._has_channel_tools_access(update, context):
                await query.answer("Channel tools are only available to connected channel owners and admins.", show_alert=True)
                return

            # Welcome-menu actions are shared by /start and Mini App fallbacks.
            if callback_data == "start_back":
                await query.answer()
                await query.edit_message_text(
                    self._start_text(),
                    parse_mode=ParseMode.HTML,
                    reply_markup=self._start_keyboard(
                        update, context,
                        await self._channel_manager.should_show_button(update, context.bot),
                    ),
                )
                return
            if callback_data == "start_help":
                await query.answer()
                await query.message.reply_text(
                    self._help_text(),
                    parse_mode=ParseMode.HTML,
                    reply_markup=self._help_keyboard(),
                )
                return
            if callback_data in {"start_misc", "help_misc", "misc_home"}:
                await query.answer()
                await self._channel_manager._show_home(update, context)
                return
            if callback_data == "misc_connections":
                await query.answer()
                await query.edit_message_text(
                    self._connections_help_text(),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("← Channel Manager", callback_data="cm:home")
                    ]]),
                )
                return
            if callback_data == "misc_list_connections":
                if update.effective_chat is None or update.effective_chat.type != "private":
                    await query.answer("Open Channel Manager in a private chat with Annie.", show_alert=True)
                    return
                await query.answer()
                await query.edit_message_text(
                    await self._connected_channel_names(update, context),
                    parse_mode=ParseMode.HTML,
                    reply_markup=self._misc_keyboard(),
                )
                return
            if callback_data.startswith("disconnect_channel:"):
                await query.answer()
                _, raw_channel = callback_data.split(":", 1)
                channel = await self._private_channel_target(update, context, raw_channel)
                if channel is None:
                    return
                repository = self._get_channel_connection_repository()
                if repository is None:
                    await self._send_or_edit(
                        update, "Channel connections need cloud storage.", self._misc_keyboard()
                    )
                    return
                try:
                    removed = await asyncio.to_thread(repository.disconnect, channel.id)
                except Exception as exc:
                    logger.warning(
                        "Could not remove channel connection channel_id=%s error=%s",
                        channel.id, type(exc).__name__,
                    )
                    await self._send_or_edit(
                        update, "I couldn’t disconnect that channel. Please try again."
                    )
                    return
                if removed and self._channel_index_runtime is not None:
                    await self._channel_index_runtime.cancel_channel(channel.id)
                    await self._stop_channel_index_listener_if_idle()
                self._channel_manager.invalidate_access_cache(update.effective_user.id)
                self._schedule_user_command_scope_refresh(
                    context.bot, update.effective_user.id, force=True
                )
                notice = (
                    f"Disconnected <b>{html_escape(channel.title or 'Telegram channel')}</b>."
                    if removed else "That channel was already disconnected."
                )
                await self._show_disconnect_picker(update, context, notice)
                return
            if callback_data == "misc_index":
                await query.answer()
                await self._show_index_channel_picker(update, context)
                return
            if callback_data.startswith("index_choose:"):
                await query.answer()
                _, raw_channel, action, value = callback_data.split(":", 3)
                await self._select_index_channel(
                    update, context, int(raw_channel), action, value
                )
                return
            if callback_data.startswith("index_format:"):
                await query.answer()
                _, raw_channel = callback_data.split(":", 1)
                await self._show_index_format(update, context, int(raw_channel))
                return
            if callback_data.startswith("index_lists:"):
                await query.answer()
                _, raw_channel = callback_data.split(":", 1)
                await self._show_index_lists(update, context, int(raw_channel))
                return
            if callback_data.startswith("index_exclusions:"):
                await query.answer()
                _, raw_channel = callback_data.split(":", 1)
                await self._show_index_exclusions(update, context, int(raw_channel))
                return
            if callback_data.startswith("index_exclusion_add:"):
                await query.answer()
                _, raw_channel = callback_data.split(":", 1)
                channel_id = int(raw_channel)
                if await self._private_channel_target(update, context, raw_channel) is None:
                    return
                self._index_pending_input[update.effective_user.id] = {
                    "kind": "exclude_sender", "channel_id": channel_id,
                    "expires_at": time.time() + 600,
                }
                await query.edit_message_text(
                    "Forward a post by that sender from this channel, send their public @username, or enter the positive sender ID from Annie’s log.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                        "Cancel", callback_data=f"index_exclusion_cancel:{channel_id}"
                    )]]),
                )
                return
            if callback_data.startswith("index_exclusion_cancel:"):
                await query.answer()
                _, raw_channel = callback_data.split(":", 1)
                self._index_pending_input.pop(update.effective_user.id, None)
                await self._show_index_exclusions(update, context, int(raw_channel))
                return
            if callback_data.startswith("index_exclusion_remove:"):
                await query.answer()
                _, raw_channel, raw_sender = callback_data.split(":", 2)
                channel_id, sender_id = int(raw_channel), int(raw_sender)
                if sender_id <= 0 or await self._private_channel_target(update, context, raw_channel) is None:
                    return
                repository = self._get_channel_index_repository()
                if repository is not None:
                    await asyncio.to_thread(
                        repository.remove_excluded_sender, channel_id, sender_id
                    )
                await self._show_index_exclusions(update, context, channel_id)
                return
            if callback_data.startswith("index_toggle:"):
                await query.answer()
                _, raw_channel, raw_enabled = callback_data.split(":", 2)
                channel = await self._private_channel_target(update, context, raw_channel)
                if channel is None:
                    return
                repository = self._get_channel_index_repository()
                if repository is None:
                    await self._send_or_edit(update, "Channel indexes need MongoDB storage.", self._misc_keyboard())
                    return
                channel_id, enabled = int(raw_channel), raw_enabled == "1"
                current = await asyncio.to_thread(repository.get, channel_id) or {}
                if enabled and not current.get("targets"):
                    await self._send_or_edit(update, "Register an existing index post before turning updates on.", self._index_settings_keyboard(channel_id, current))
                    return
                if enabled and not await self._ensure_channel_index_listener():
                    await self._send_or_edit(update, "Automatic updates need API_ID and API_HASH and must run in polling mode. Annie could not start the index listener, so updates remain off.", self._index_settings_keyboard(channel_id, current))
                    return
                await asyncio.to_thread(repository.update, channel_id, {"enabled": enabled})
                if not enabled and self._channel_index_runtime is not None:
                    await self._channel_index_runtime.cancel_channel(channel_id)
                    await self._stop_channel_index_listener_if_idle()
                await self._show_index_settings(update, context, channel_id)
                return
            if callback_data.startswith("index_delay:"):
                await query.answer()
                _, raw_channel, raw_delay = callback_data.split(":", 2)
                if await self._private_channel_target(update, context, raw_channel) is None:
                    return
                repository = self._get_channel_index_repository()
                if repository:
                    await asyncio.to_thread(repository.update, int(raw_channel), {"delay_minutes": int(raw_delay)})
                await self._show_index_settings(update, context, int(raw_channel))
                return
            if callback_data.startswith("index_sort:"):
                await query.answer()
                _, raw_channel, sort_order = callback_data.split(":", 2)
                channel_id = int(raw_channel)
                if await self._private_channel_target(update, context, raw_channel) is None:
                    return
                if sort_order not in {"alphabetical", "added"}:
                    return
                repository = self._get_channel_index_repository()
                if repository is not None:
                    await asyncio.to_thread(repository.update, channel_id, {"sort_order": sort_order})
                    config = await asyncio.to_thread(repository.get, channel_id) or {}
                    entries = list(config.get("entries") or [])
                    for target in config.get("targets") or []:
                        target_id = int(target["message_id"])
                        target_entries = [
                            entry for entry in entries
                            if int(entry.get("target_message_id", -1)) == target_id
                        ]
                        text = ChannelIndexRuntime._render_target(
                            str(target.get("base_html") or ""), target_entries, sort_order,
                            str(target.get("entry_bullet") or config.get("entry_bullet") or "🔹"),
                        )
                        try:
                            await context.bot.edit_message_text(
                                chat_id=channel_id, message_id=target_id,
                                text=text, parse_mode="HTML", disable_web_page_preview=True,
                            )
                        except BadRequest as exc:
                            if "message is not modified" not in str(exc).casefold():
                                logger.warning(
                                    "[channel-index] could not apply entry order channel_id=%s index_post_id=%s error=%s",
                                    channel_id, target_id, type(exc).__name__,
                                )
                        except Exception as exc:
                            logger.warning(
                                "[channel-index] could not apply entry order channel_id=%s index_post_id=%s error=%s",
                                channel_id, target_id, type(exc).__name__,
                            )
                await self._show_index_format(update, context, channel_id)
                return
            if callback_data.startswith("index_bullet:"):
                await query.answer()
                _, raw_channel = callback_data.split(":", 1)
                if await self._private_channel_target(update, context, raw_channel) is None:
                    return
                channel_id = int(raw_channel)
                self._index_pending_input[update.effective_user.id] = {
                    "kind": "bullet", "channel_id": channel_id,
                    "expires_at": time.time() + 600, "return_to": "format",
                }
                await query.edit_message_text(
                    "Send one emoji or symbol for each entry, such as • or 📚. This replaces the current bullet.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                        "Cancel", callback_data=f"index_bullet_cancel:{channel_id}"
                    )]]),
                )
                return
            if callback_data.startswith("index_bullet_target:"):
                await query.answer()
                _, raw_channel = callback_data.split(":", 1)
                if await self._private_channel_target(update, context, raw_channel) is None:
                    return
                channel_id = int(raw_channel)
                self._index_pending_input[update.effective_user.id] = {
                    "kind": "bullet_target", "channel_id": channel_id,
                    "expires_at": time.time() + 600, "return_to": "lists",
                }
                await query.edit_message_text(
                    "Forward the registered placeholder you want to customize. Annie will then ask for its bullet.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                        "Cancel", callback_data=f"index_bullet_cancel:{channel_id}"
                    )]]),
                )
                return
            if callback_data.startswith("index_bullet_cancel:"):
                await query.answer()
                _, raw_channel = callback_data.split(":", 1)
                state = self._index_pending_input.pop(update.effective_user.id, None) or {}
                if state.get("return_to") == "lists":
                    await self._show_index_lists(update, context, int(raw_channel))
                else:
                    await self._show_index_format(update, context, int(raw_channel))
                return
            if callback_data.startswith("index_entry_source:"):
                await query.answer()
                _, raw_channel, mode = callback_data.split(":", 2)
                if await self._private_channel_target(update, context, raw_channel) is None:
                    return
                if mode not in {"text", "hashtags", "links", "image", "file", "prefix"}:
                    return
                repository = self._get_channel_index_repository()
                current = await asyncio.to_thread(repository.get, int(raw_channel)) if repository else {}
                current = current or {}
                prefix = str(current.get("entry_prefix") or current.get("title_prefix") or "")
                if mode == "prefix" and not prefix:
                    self._index_pending_input[update.effective_user.id] = {
                        "kind": "prefix", "channel_id": int(raw_channel),
                        "expires_at": time.time() + 600,
                    }
                    await query.edit_message_text(
                        "Custom prefix finds a text/caption line that begins with your marker, then uses the text after it. Send the marker now, for example <code>Title:</code>.",
                        parse_mode=ParseMode.HTML,
                    )
                    return
                if repository:
                    await asyncio.to_thread(repository.update, int(raw_channel), {"entry_mode": mode})
                await self._show_index_format(update, context, int(raw_channel))
                return
            if callback_data.startswith("index_mode:"):
                await query.answer()
                _, raw_channel, mode = callback_data.split(":", 2)
                if await self._private_channel_target(update, context, raw_channel) is None:
                    return
                repository = self._get_channel_index_repository()
                current = await asyncio.to_thread(repository.get, int(raw_channel)) if repository else {}
                current = current or {}
                if mode == "prefix" and not (current.get("entry_prefix") or current.get("title_prefix")):
                    self._index_pending_input[update.effective_user.id] = {
                        "kind": "prefix", "channel_id": int(raw_channel),
                        "expires_at": time.time() + 600,
                    }
                    await query.edit_message_text(
                        "Custom prefix finds a text/caption line that begins with your marker, then uses the text after it. Send the marker now, for example <code>Title:</code>.",
                        parse_mode=ParseMode.HTML,
                    )
                    return
                if repository:
                    await asyncio.to_thread(repository.update, int(raw_channel), {"entry_mode": "prefix" if mode == "prefix" else "text"})
                await self._show_index_format(update, context, int(raw_channel))
                return
            if callback_data.startswith("index_prefix:"):
                await query.answer()
                _, raw_channel = callback_data.split(":", 1)
                if await self._private_channel_target(update, context, raw_channel) is None:
                    return
                self._index_pending_input[update.effective_user.id] = {
                    "kind": "prefix", "channel_id": int(raw_channel),
                    "expires_at": time.time() + 600, "return_to": "format",
                }
                await query.edit_message_text(
                    "Send a text/caption prefix, such as <code>Title:</code>. Annie uses the text after it as the entry label.",
                    parse_mode=ParseMode.HTML,
                )
                return
            if callback_data.startswith("index_targets:"):
                await query.answer()
                _, raw_channel, mode = callback_data.split(":", 2)
                channel = await self._private_channel_target(update, context, raw_channel)
                if channel is None:
                    return
                try:
                    bot_member = await context.bot.get_chat_member(channel.id, context.bot.id)
                except Exception:
                    bot_member = None
                bot_status = getattr(bot_member, "status", "")
                can_edit = bot_status == "creator" or bool(getattr(bot_member, "can_edit_messages", False))
                if bot_status not in ("administrator", "creator") or not can_edit:
                    await self._send_or_edit(update, "Annie needs channel admin permission to edit index posts.", self._misc_keyboard())
                    return
                self._index_pending_input[update.effective_user.id] = {
                    "kind": "targets", "mode": mode, "channel_id": channel.id,
                    "targets": [], "expires_at": time.time() + 600,
                }
                await query.edit_message_text(
                    "Forward one or more existing index posts from this channel. You can send several together or one at a time. Tap Done after the last one to register them (up to 12).",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                        "Cancel", callback_data=f"index_cancel:{channel.id}"
                    )]]),
                )
                return
            if callback_data.startswith("index_cancel:"):
                await query.answer()
                _, raw_channel = callback_data.split(":", 1)
                self._index_pending_input.pop(update.effective_user.id, None)
                await self._show_index_lists(update, context, int(raw_channel))
                return
            if callback_data.startswith("index_targets_done:"):
                _, raw_channel = callback_data.split(":", 1)
                channel_id = int(raw_channel)
                state = self._index_pending_input.get(update.effective_user.id) or {}
                if state.get("kind") != "targets" or int(state.get("channel_id", 0)) != channel_id:
                    await query.answer("That setup step expired. Run /index again.", show_alert=True)
                    return
                if await self._private_channel_target(update, context, raw_channel) is None:
                    return
                targets = state.get("targets") or []
                repository = self._get_channel_index_repository()
                if not targets or repository is None:
                    await query.answer("Forward at least one index post first.", show_alert=True)
                    return
                if state.get("mode") == "add":
                    current = await asyncio.to_thread(repository.get, channel_id) or {}
                    if len(current.get("targets") or []) + len(targets) > 12:
                        await query.answer("A maximum of 12 index posts can be registered.", show_alert=True)
                        return
                await asyncio.to_thread(repository.set_targets, channel_id, targets, state.get("mode") == "add")
                if self._channel_index_runtime is not None:
                    await self._channel_index_runtime.resume_pending(channel_id)
                self._index_pending_input.pop(update.effective_user.id, None)
                await query.answer()
                await self._show_index_lists(update, context, channel_id)
                return
            if callback_data.startswith("index_categories:"):
                await query.answer()
                _, raw_channel = callback_data.split(":", 1)
                await self._show_index_categories(update, context, int(raw_channel))
                return
            if callback_data.startswith("index_category_set:"):
                await query.answer()
                _, raw_channel, raw_target = callback_data.split(":", 2)
                if await self._private_channel_target(update, context, raw_channel) is None:
                    return
                self._index_pending_input[update.effective_user.id] = {
                    "kind": "category", "channel_id": int(raw_channel),
                    "target_id": int(raw_target), "expires_at": time.time() + 600,
                }
                await query.edit_message_text(
                    "Send a short marker that appears in posts for this list. It can be any text you choose, such as <code>#Manga</code> or <code>Anime:</code>. Annie will match it in post text or captions.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                        "Cancel", callback_data=f"index_category_cancel:{raw_channel}"
                    )]]),
                )
                return
            if callback_data.startswith("index_category_cancel:"):
                await query.answer()
                _, raw_channel = callback_data.split(":", 1)
                self._index_pending_input.pop(update.effective_user.id, None)
                await self._show_index_categories(update, context, int(raw_channel))
                return
            if callback_data.startswith("index_category_clear:"):
                await query.answer()
                _, raw_channel, raw_target = callback_data.split(":", 2)
                if await self._private_channel_target(update, context, raw_channel) is None:
                    return
                repository = self._get_channel_index_repository()
                if repository:
                    config = await asyncio.to_thread(repository.get, int(raw_channel)) or {}
                    targets = list(config.get("targets") or [])
                    for target in targets:
                        if int(target.get("message_id", -1)) == int(raw_target):
                            target.pop("category_marker", None)
                    await asyncio.to_thread(repository.update, int(raw_channel), {"targets": targets})
                await self._show_index_categories(update, context, int(raw_channel))
                return
            if callback_data.startswith("index_import_done:"):
                await query.answer()
                _, raw_channel = callback_data.split(":", 1)
                channel_id = int(raw_channel)
                state = self._index_pending_input.get(update.effective_user.id) or {}
                if state.get("kind") == "backfill" and int(state.get("channel_id", 0)) == channel_id:
                    self._index_pending_input.pop(update.effective_user.id, None)
                if await self._private_channel_target(update, context, raw_channel) is None:
                    return
                await self._show_index_lists(update, context, channel_id)
                return
            if callback_data.startswith("index_backfill:"):
                await query.answer()
                _, raw_channel = callback_data.split(":", 1)
                channel_id = int(raw_channel)
                if await self._private_channel_target(update, context, raw_channel) is None:
                    return
                repository = self._get_channel_index_repository()
                config = await asyncio.to_thread(repository.get, channel_id) if repository else None
                if not config or not config.get("enabled"):
                    await self._send_or_edit(
                        update, "Turn Index updates on first.",
                        self._index_lists_keyboard(channel_id, config),
                    )
                    return
                if self._channel_index_runtime is None:
                    await self._send_or_edit(update, "The Index listener is not running. Turn updates off and on, then try again.", self._index_lists_keyboard(channel_id, config))
                    return
                self._index_pending_input[update.effective_user.id] = {
                    "kind": "backfill", "channel_id": channel_id,
                    "expires_at": time.time() + 3600,
                }
                await query.edit_message_text(
                    "Forward older posts from this channel to Annie here. She’ll add matching posts to the right Index list and skip duplicates. Tap Done when finished.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                        "Done", callback_data=f"index_import_done:{channel_id}"
                    )]]),
                )
                return
            if callback_data.startswith("index_clear:"):
                await query.answer()
                _, raw_channel = callback_data.split(":", 1)
                if await self._private_channel_target(update, context, raw_channel) is None:
                    return
                repository = self._get_channel_index_repository()
                if repository:
                    await asyncio.to_thread(repository.update, int(raw_channel), {"targets": [], "entries": [], "enabled": False})
                if self._channel_index_runtime is not None:
                    await self._channel_index_runtime.cancel_channel(int(raw_channel))
                    await self._stop_channel_index_listener_if_idle()
                await self._show_index_lists(update, context, int(raw_channel))
                return
            if callback_data == "help_main":
                await query.answer()
                await query.edit_message_text(
                    self._help_text(),
                    parse_mode=ParseMode.HTML,
                    reply_markup=self._help_keyboard(),
                )
                return
            if callback_data == "start_features":
                await query.answer()
                await query.message.reply_text(
                    self._features_text(),
                    parse_mode=ParseMode.HTML,
                    reply_markup=self._back_to_start_markup(),
                )
                return
            if callback_data == "start_portal":
                await query.answer()
                await self._send_mini_app(update, context)
                return
            if callback_data == "start_recommendations":
                await query.answer()
                await self._send_mini_app(update, context, "recommendations")
                return
            if callback_data == "start_bookshelf":
                await query.answer()
                await self._send_mini_app(update, context, "bookshelf")
                return
            if callback_data == "start_favorites":
                await query.answer()
                await self._send_mini_app(update, context, "favorites")
                return

            # All normal search-result controls carry the original requester's
            # ID. Reject another group member before any cache/state is touched.
            if callback_data.startswith(("page_", "book_", "back_", "download_", "close_")):
                try:
                    requester_id = int(callback_data.split("_")[1])
                except (IndexError, ValueError):
                    await query.answer("This button has expired.", show_alert=True)
                    return
                if update.effective_user.id != requester_id:
                    await query.answer("These search results belong to another user.", show_alert=True)
                    return

            if callback_data.startswith("close_"):
                self._clear_active_result_message(
                    (query.message.chat_id, update.effective_user.id), query.message.message_id
                )
                try:
                    await query.delete_message()
                except Exception as exc:
                    logger.info("Group result message could not be deleted: %s", exc)
                await query.answer()
                return

            # ── Pagination ───────────────────────────────────────────────────
            if callback_data.startswith("page_"):
                parts = callback_data.split("_")
                user_id = int(parts[1])
                page_num = int(parts[2])

                chat_id = query.message.chat_id
                message_id = query.message.message_id
                result_state = self._get_result_message_state(
                    chat_id, message_id, user_id
                )
                books = (
                    result_state.get("books") if result_state
                    else self._get_cached_books(user_id)
                )
                if books is None:
                    await query.answer("Search results expired.", show_alert=True)
                    return

                self._search_page_cache[user_id] = page_num
                query_text = (
                    result_state.get("query", "") if result_state
                    else self._search_query_cache.get(user_id, "")
                )
                self._search_query_cache[user_id] = query_text
                self._set_cached_books(user_id, books)
                self._cache_result_message(
                    chat_id, message_id, user_id, books, query_text, page_num
                )
                self._active_result_messages[(chat_id, user_id)] = {
                    "message_id": message_id,
                    "query": query_text,
                    "page": page_num,
                }
                results_text, keyboard = self._build_search_results_message(
                    books, query_text, user_id, page_num, 5
                )
                try:
                    await query.edit_message_text(
                        text=results_text, reply_markup=keyboard, parse_mode=ParseMode.HTML
                    )
                except BadRequest as exc:
                    # Telegram raises when a user taps the already-selected
                    # page button. Treat it as a harmless no-op so pagination
                    # remains responsive and the callback spinner clears.
                    if "message is not modified" not in str(exc).casefold():
                        raise
                await query.answer()
                self._schedule_result_rating_refresh(
                    books, query_text, user_id, page_num, chat_id,
                    message_id, context.bot,
                )
                return

            # ── Back to results ────────────────────────────────────────────────
            if callback_data.startswith("back_"):
                parts = callback_data.split("_")
                user_id = int(parts[1])

                current_state = self._get_result_message_state(
                    query.message.chat_id, query.message.message_id, user_id
                )
                books = (
                    current_state.get("books") if current_state
                    else self._get_cached_books(user_id)
                )
                if books is None:
                    await query.answer("Search results expired.", show_alert=True)
                    return

                page_num = (
                    current_state.get("page", 1) if current_state
                    else self._search_page_cache.get(user_id, 1)
                )
                query_text = (
                    current_state.get("query", "") if current_state
                    else self._search_query_cache.get(user_id, "")
                )
                results_text, keyboard = self._build_search_results_message(
                    books, query_text, user_id, page_num=page_num, page_size=5
                )

                # Delete the current message (could be a photo or text) and send
                # a clean, fresh text-only message with the results list.
                self._clear_active_result_message(
                    (query.message.chat_id, user_id), query.message.message_id
                )
                await query.delete_message()
                result_message = await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=results_text,
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML,
                )
                self._cache_result_message(
                    query.message.chat_id, result_message.message_id,
                    user_id, books, query_text, page_num,
                )
                self._active_result_messages[(query.message.chat_id, user_id)] = {
                    "message_id": result_message.message_id,
                    "query": query_text,
                    "page": page_num,
                }
                self._schedule_result_rating_refresh(
                    books, query_text, user_id, page_num, query.message.chat_id,
                    result_message.message_id, context.bot,
                )
                return

            # ── Download cover ────────────────────────────────────────────────
            if callback_data.startswith("download_"):
                if getattr(query.message.chat, "type", "private") != "private":
                    await query.answer("Cover downloads are available in private chat only.", show_alert=True)
                    return
                parts = callback_data.split("_")
                user_id = int(parts[1])
                book_idx = int(parts[2])

                result_state = self._get_result_message_state(
                    query.message.chat_id, query.message.message_id, user_id
                )
                books = (
                    result_state.get("books") if result_state
                    else self._get_cached_books(user_id)
                )
                if books is None:
                    await query.answer("Search results expired.", show_alert=True)
                    return

                if book_idx >= len(books):
                    await query.answer("Invalid selection.", show_alert=True)
                    return

                book = books[book_idx]
                cover_url = book.get("cover_url")

                if not cover_url:
                    await query.answer("No cover image available.", show_alert=True)
                    return

                temp_file = await asyncio.to_thread(self.download_and_save_image, cover_url, book)
                if not temp_file:
                    await query.answer("Failed to download cover.", show_alert=True)
                    return

                try:
                    with open(temp_file, "rb") as f:
                        await context.bot.send_document(
                            chat_id=query.message.chat_id,
                            document=f,
                            filename=f"{book['title'][:40].replace(' ', '_')}_cover.jpg",
                            caption=f"📖 <b>{html_escape(book['title'])}</b>\n{html_escape(book['author'])}",
                            parse_mode=ParseMode.HTML,
                        )
                finally:
                    self.cleanup_temp_file(temp_file)
                return

            # ── Inline result View More action (one-time expansion) ──────────
            if callback_data.startswith(("inline_more_", "hourglass_")):
                prefix = "hourglass_" if callback_data.startswith("hourglass_") else "inline_more_"
                callback_key = callback_data[len(prefix):]

                # Get book data from callback cache
                book_data = self._get_inline_callback_data(callback_key)
                if not book_data:
                    await query.answer("Book data expired.", show_alert=True)
                    return

                # Ensure we have complete data (blocking I/O, run in thread to avoid blocking event loop)
                book_data, _ = await asyncio.to_thread(MultiSourceBookAggregator._ensure_ratings, book_data)
                book_data = await asyncio.to_thread(MultiSourceBookAggregator._ensure_cover, book_data, hc_data=_)

                # Fill missing bibliographic fields (ISBN, pages, year) by looking up
                # the book on Hardcover.  This only runs when at least one of those
                # fields is absent (e.g. iTunes-only inline results) and only for the
                # ONE book the user selected — it does not affect inline search speed.
                if not book_data.get("isbn") or not book_data.get("page_count") or not book_data.get("published_date"):
                    hc_books = await asyncio.to_thread(
                        MultiSourceBookAggregator.search_hardcover,
                        f"{book_data.get('title', '')} {book_data.get('author', '')}".strip(),
                        5,
                    )
                    if hc_books:
                        # Pick best match by title+author similarity, preferring metadata-rich results
                        title_lower = (book_data.get("title") or "").lower()
                        author_lower = (book_data.get("author") or "").lower()

                        def _metadata_richness(book: dict) -> int:
                            """Score how much useful metadata a Hardcover result has."""
                            score = 0
                            if book.get("isbn"): score += 2
                            if book.get("page_count"): score += 2
                            if book.get("published_date"): score += 2
                            if book.get("categories") or book.get("genres"): score += 2
                            if book.get("description"): score += 1
                            if book.get("cover_url"): score += 1
                            if book.get("rating") and book.get("rating") > 0: score += 1
                            if book.get("rating_count") and book.get("rating_count") > 0: score += 1
                            return score

                        best, best_score, best_richness = None, -1, -1
                        for hb in hc_books:
                            hb_title = (hb.get("title") or "").lower()
                            hb_author = (hb.get("author") or "").lower()
                            # Primary: title match (2) + author match (2) = max 4
                            title_match = 2 if (title_lower in hb_title or hb_title in title_lower) else 0
                            author_match = 2 if (author_lower in hb_author or hb_author in author_lower) else 0
                            primary_score = title_match + author_match
                            # Secondary: metadata richness (max 12)
                            richness = _metadata_richness(hb)
                            # Combined: primary dominates, richness breaks ties
                            combined = (primary_score << 8) + richness
                            if combined > best_score:
                                best_score = combined
                                best_richness = richness
                                best = hb
                        if best and best_score >= 256:  # At least one of title/author matched
                            if not book_data.get("isbn") and best.get("isbn"):
                                book_data["isbn"] = best["isbn"]
                            if not book_data.get("page_count") and best.get("page_count"):
                                book_data["page_count"] = best["page_count"]
                            if not book_data.get("published_date") and best.get("published_date"):
                                book_data["published_date"] = best["published_date"]
                            # Also merge genres/categories, description, cover, and rating
                            # if the selected book is missing them
                            if not book_data.get("categories") and best.get("categories"):
                                book_data["categories"] = best["categories"]
                                book_data["genres"] = best.get("genres", best["categories"])
                            if not book_data.get("description") and best.get("description"):
                                book_data["description"] = best["description"]
                            if not book_data.get("cover_url") and best.get("cover_url"):
                                book_data["cover_url"] = best["cover_url"]
                            if not book_data.get("rating") and best.get("rating"):
                                book_data["rating"] = best["rating"]
                                book_data["rating_count"] = best.get("rating_count", 0)
                                book_data["rating_formatted"] = best.get("rating_formatted", f"{best['rating']:.2f}")
                                book_data["rating_source"] = best.get("rating_source", "hardcover")

                # Keep inline search fast: translate only the selected book's
                # title/description when the user opens View More.
                await self._translate_book_text_fields(book_data)
                self._set_inline_callback_data(callback_key, book_data)

                # Build expanded caption
                expanded_caption = self._build_expanded_inline_caption(book_data)

                # Preserve the original one-time expansion behavior: after
                # opening details, leave only the Goodreads link available.
                expanded_keyboard = self._build_goodreads_keyboard(book_data)

                # Restore cover image: the inline Article produces a text-only
                # message, so convert it into a photo message carrying the cover
                # and the expanded caption in a single call.
                cover_url = book_data.get("cover_url")
                if cover_url and not cover_url.startswith("data:"):
                    try:
                        media = InputMediaPhoto(
                            media=cover_url,
                            caption=expanded_caption,
                            parse_mode=ParseMode.HTML,
                        )
                        await query.edit_message_media(
                            media=media,
                            reply_markup=expanded_keyboard,
                        )
                        logger.info(
                            f"Inline details expanded (photo) for: {book_data.get('title', 'Unknown')}"
                        )
                        await query.answer()
                        return
                    except Exception as media_error:
                        logger.warning(
                            f"Failed to restore photo media, falling back to text: {media_error}"
                        )
                        # Fall through to text-caption / text edits below.

                # Edit the inline message (same cover, new caption and keyboard)
                try:
                    await query.edit_message_caption(
                        caption=expanded_caption,
                        parse_mode=ParseMode.HTML,
                        reply_markup=expanded_keyboard
                    )
                    logger.info(f"Inline details expanded for: {book_data.get('title', 'Unknown')}")
                except Exception as e:
                    logger.warning(f"Failed to edit inline message caption: {e}")
                    # Fallback: try to edit message text if caption edit fails
                    try:
                        await query.edit_message_text(
                            text=expanded_caption,
                            parse_mode=ParseMode.HTML,
                            reply_markup=expanded_keyboard
                        )
                    except Exception as e2:
                        logger.error(f"Failed to edit inline message: {e2}")
                        await query.answer("Failed to update message.", show_alert=True)
                        return
                await query.answer()
                return

            # ── Clarification callbacks ──────────────────────────────────────
            if callback_data.startswith(("clar_yes_", "clar_no_", "clar_cancel_")):
                action, owner_text = callback_data.rsplit("_", 1)
                try:
                    user_id = int(owner_text)
                except ValueError:
                    await query.answer("This clarification prompt has expired.", show_alert=True)
                    return
                entry = self._clarification.get(user_id)
                if not entry:
                    await query.answer("This clarification prompt has expired.", show_alert=True)
                    return

                callback_user_id = update.effective_user.id
                callback_chat_id = update.effective_chat.id
                callback_message_id = query.message.message_id if query.message else None
                if (callback_user_id != user_id
                        or callback_user_id != entry.get("requester_id")
                        or callback_chat_id != entry.get("chat_id")
                        or callback_message_id != entry.get("message_id")):
                    await query.answer(
                        "This clarification prompt is for another user.", show_alert=True
                    )
                    return

                cancel_exempt = (
                    action == "clar_cancel"
                    and await self._is_owner_or_group_admin(user_id, update.effective_chat)
                )
                self._clarification.pop(user_id, None)
                try:
                    await query.delete_message()
                except Exception as exc:
                    # Continue the selected action if the prompt was already removed.
                    logger.info("Clarification prompt could not be deleted: %s", exc)

                if action == "clar_cancel":
                    abuse_action = self._record_clarification_cancel(user_id, exempt=cancel_exempt)
                    reply_text = (
                        "❌ Search cancelled. Please start a new search with a different query using /search."
                    )
                    if abuse_action == "cooldown":
                        reply_text += "\n\n⏳ You have reached the cancellation limit. Searches are paused for 5 minutes."
                    elif abuse_action == "blocked":
                        reply_text += "\n\n🚫 Repeated cancellations have paused your searches for 1 hour."
                    send_kwargs = {
                        "chat_id": entry["chat_id"],
                        "text": reply_text,
                        "reply_to_message_id": entry.get("source_message_id"),
                    }
                    try:
                        await self.app.bot.send_message(**send_kwargs)
                    except Exception:
                        # Fall back to a normal chat message if the source was removed.
                        send_kwargs.pop("reply_to_message_id", None)
                        await self.app.bot.send_message(**send_kwargs)
                    await query.answer()
                    return

                confirmed = action == "clar_yes"
                self._handle_clarification_response(update, context, confirmed, entry=entry)
                await query.answer(
                    "Searching for that book..." if confirmed else "Searching your query..."
                )
                return

            # ── Book selection ────────────────────────────────────────────────
            if not callback_data.startswith("book_"):
                return

            parts = callback_data.split("_")
            user_id = int(parts[1])
            book_idx = int(parts[2])
            # page_num is encoded in callback as 4th part (for Back to Results restoration)
            page_num = int(parts[3]) if len(parts) > 3 else 1
            self._search_page_cache[user_id] = page_num
            result_state = self._get_result_message_state(
                query.message.chat_id, query.message.message_id, user_id
            )
            if result_state:
                page_num = result_state.get("page", page_num)
                self._search_page_cache[user_id] = page_num
                self._search_query_cache[user_id] = result_state.get("query", "")
            self._clear_active_result_message(
                (query.message.chat_id, user_id), query.message.message_id
            )

            books = (
                result_state.get("books") if result_state
                else self._get_cached_books(user_id)
            )
            if books is None:
                await query.edit_message_text("❌ Search results expired.", parse_mode=ParseMode.HTML)
                return

            if book_idx >= len(books):
                await query.edit_message_text("❌ Invalid selection.", parse_mode=ParseMode.HTML)
                return

            book = books[book_idx]

            # Fetch ratings lazily (saves Hardcover API quota — not fetched during search).
            # _ensure_ratings also returns the cached Hardcover data so _ensure_cover
            # can reuse it without a redundant API call.
            # Blocking I/O, run in thread to avoid blocking the event loop.
            book, hc_data = await asyncio.to_thread(MultiSourceBookAggregator._ensure_ratings, book)

            # Source a real cover if Google Books only offered its placeholder.
            # Pass hc_data to avoid re-fetching from Hardcover.
            book = await asyncio.to_thread(MultiSourceBookAggregator._ensure_cover, book, hc_data=hc_data)

            # Translate non-English text only after the user selects a book.
            await self._translate_book_text_fields(book)

            text_info = self.format_book_message(book)

            await query.delete_message()

            temp_file = None
            detail_message = None
            cover_url = book.get("cover_url")

            if cover_url:
                temp_file = await asyncio.to_thread(self.download_and_save_image, cover_url, book)

            # Some edition records (for example illustrated/anniversary editions)
            # have only a catalog placeholder even though another exact-work
            # result in this same search has a usable cover. Use that cover only
            # when provider-specific recovery above found nothing.
            if not temp_file:
                def _base_work_title(value: str) -> str:
                    value = re.sub(
                        r"\s*[:(]\s*(?:the\s+)?(?:minalima|illustrated|special|deluxe|collector(?:'s)?|anniversary|paperback|hardcover|ebook|e-book|edition)\b.*$",
                        "",
                        value or "",
                        flags=re.IGNORECASE,
                    )
                    return re.sub(r"[^a-z0-9]+", "", value.lower())

                current_author = re.sub(r"[^a-z0-9]+", "", (book.get("author") or "").lower())
                current_work = _base_work_title(book.get("title", ""))
                for sibling in books:
                    if sibling is book or not sibling.get("cover_url"):
                        continue
                    sibling_author = re.sub(
                        r"[^a-z0-9]+", "", (sibling.get("author") or "").lower()
                    )
                    if (
                        current_work
                        and current_work == _base_work_title(sibling.get("title", ""))
                        and current_author
                        and current_author == sibling_author
                    ):
                        sibling_source = dict(book)
                        sibling_source["cover_url"] = sibling["cover_url"]
                        sibling_source["cover_source"] = sibling.get(
                            "cover_source", sibling.get("source", "catalog edition")
                        )
                        temp_file = await asyncio.to_thread(
                            self.download_and_save_image,
                            sibling["cover_url"],
                            sibling_source,
                        )
                        if temp_file:
                            book["cover_url"] = sibling["cover_url"]
                            book["cover_source"] = sibling_source["cover_source"]
                            logger.info(
                                "Using sibling-edition cover for %s from %s",
                                book.get("title", "Unknown"),
                                sibling.get("title", "Unknown"),
                            )
                            break

            if temp_file:
                # ── TEMP FILE DIAGNOSTICS ──
                import os as _os
                _fpath = temp_file
                _fsize = _os.path.getsize(_fpath) if _os.path.exists(_fpath) else -1
                _fhex = ""
                _freadable = False
                _fpos_after_open = -1
                _pil_fmt = ""
                _pil_dims = ""
                try:
                    with open(_fpath, "rb") as _tf:
                        _fhex = _tf.read(16).hex()
                        _tf.seek(0)
                        _freadable = len(_tf.read(1)) == 1
                        _tf.seek(0)
                        _fpos_after_open = _tf.tell()
                        # PIL format/dims if already available (Pillow is in requirements.txt)
                        try:
                            from PIL import Image as _PILImg
                            with open(_fpath, "rb") as _pf:
                                _pil_img = _PILImg.open(_pf)
                                _pil_fmt = _pil_img.format or "UNKNOWN"
                                _pil_dims = f"{_pil_img.width}x{_pil_img.height}"
                        except Exception:
                            pass
                except Exception as _e:
                    _fhex = f"<read error: {_e}>"
                logger.info(
                    f"COVER DIAG: path={_fpath} size={_fsize} first16={_fhex} "
                    f"readable={_freadable} fpos={_fpos_after_open} "
                    f"pil_fmt={_pil_fmt} pil_dims={_pil_dims}"
                )
                # ── END DIAGNOSTICS ──

                try:
                    reply_markup = self._build_detail_keyboard(
                        user_id, book_idx, getattr(query.message.chat, "type", "private")
                    )

                    logger.info(f"ABOUT TO SEND COVER: path={_fpath} size={_fsize} position={_fpos_after_open}")
                    with open(temp_file, "rb") as f:
                        detail_message = await context.bot.send_photo(
                            chat_id=query.message.chat_id,
                            photo=f,
                            caption=text_info,
                            parse_mode=ParseMode.HTML,
                            reply_markup=reply_markup,
                        )
                    logger.info("COVER SEND COMPLETED")
                    logger.info(f"✅ Sent book: {book['title']}")
                except Exception as e:
                    logger.warning(f"Could not send photo: {e}")
                    reply_markup = self._build_detail_keyboard(
                        user_id, book_idx, getattr(query.message.chat, "type", "private")
                    )
                    detail_message = await context.bot.send_message(
                        chat_id=query.message.chat_id,
                        text=text_info,
                        parse_mode=ParseMode.HTML,
                        reply_markup=reply_markup,
                    )
                finally:
                    self.cleanup_temp_file(temp_file)
            else:
                reply_markup = self._build_detail_keyboard(
                    user_id, book_idx, getattr(query.message.chat, "type", "private")
                )
                detail_message = await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=text_info,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )

            if result_state and detail_message:
                self._cache_result_message(
                    query.message.chat_id, detail_message.message_id, user_id,
                    books, result_state.get("query", ""), page_num,
                )

            # ── Hourglass button (inline details expansion) ─────────────────────
            if callback_data.startswith("hourglass_"):
                # Extract callback key from hourglass_<callback_key>
                callback_key = callback_data[10:]  # Remove "hourglass_" prefix

                # Get book data from callback cache
                book_data = self._get_inline_callback_data(callback_key)
                if not book_data:
                    await query.answer("Book data expired.", show_alert=True)
                    return

                # Ensure we have complete data (blocking I/O, run in thread to avoid blocking event loop)
                book_data, _ = await asyncio.to_thread(MultiSourceBookAggregator._ensure_ratings, book_data)
                book_data = await asyncio.to_thread(MultiSourceBookAggregator._ensure_cover, book_data, hc_data=_)

                # Build expanded caption
                expanded_caption = self._build_expanded_inline_caption(book_data)

                # Build Goodreads keyboard
                gr_keyboard = self._build_goodreads_keyboard(book_data)

                # Edit the inline message (same cover, new caption and keyboard)
                try:
                    await query.edit_message_caption(
                        caption=expanded_caption,
                        parse_mode=ParseMode.HTML,
                        reply_markup=gr_keyboard
                    )
                    logger.info(f"Inline details expanded for: {book_data.get('title', 'Unknown')}")
                except Exception as e:
                    logger.warning(f"Failed to edit inline message caption: {e}")
                    # Fallback: try to edit message text if caption edit fails
                    try:
                        await query.edit_message_text(
                            text=expanded_caption,
                            parse_mode=ParseMode.HTML,
                            reply_markup=gr_keyboard
                        )
                    except Exception as e2:
                        logger.error(f"Failed to edit inline message: {e2}")
                        await query.answer("Failed to update message.", show_alert=True)

        except Exception as e:
            logger.error(f"Error in button_callback: {e}", exc_info=True)
            await query.answer("❌ An error occurred", show_alert=True)

    # ── Expanded inline helpers ────────────────────────────────────────────────

    def _build_expanded_inline_caption(self, book: dict) -> str:
        """Build expanded caption for when hourglass button is pressed."""
        title = html_escape(book.get("title", "Unknown"))
        author = html_escape(book.get("author", "Unknown"))
        isbn = html_escape(book.get("isbn", ""))
        pages = book.get("page_count", 0)
        published_date = book.get("published_date", "")
        year = published_date[:4] if published_date else ""
        lang = book.get("language", "")
        publisher = html_escape(book.get("publisher", ""))
        asin = book.get("asin", "")

        # Start with required header
        parts = [
            f"📖 <b>Title:</b> {title}",
            f"✍️ <b>Author:</b> {author}",
            "",  # blank line
        ]
        translated_title = str(book.get("translated_title") or "").strip()
        if translated_title and translated_title.casefold() != str(book.get("title") or "").strip().casefold():
            parts.insert(1, f"🌐 <i>English title: {html_escape(translated_title)}</i>")

        # Genres: limit to 5, remove duplicates
        categories = GoodreadsBot._unique_book_categories(book.get("categories", []))
        if categories:
            # Limit to 5
            limited_categories = categories[:5]
            genres_str = ", ".join(limited_categories)
            parts.append(f"🏷️ <b>Genres:</b> {html_escape(genres_str)}")

        # Rating
        rating = book.get("rating_formatted") or book.get("rating")
        rating_cnt = book.get("rating_count", 0)
        rating_reviews = book.get("rating_reviews", 0)
        if rating and rating_cnt:
            try:
                rating_num = float(str(rating).replace(",", "."))
                stars = "⭐" * min(int(rating_num), 5)
                parts.append(
                    f"⭐ <b>Rating:</b> {stars} <b>{html_escape(str(rating))}</b>/5 "
                    f"(<b>{rating_cnt:,}</b> ratings"
                    f"{f', {rating_reviews:,} reviews' if rating_reviews else ''})"
                )
            except ValueError:
                parts.append(
                    f"⭐ <b>Rating:</b> {html_escape(str(rating))}/5 "
                    f"(<b>{rating_cnt:,}</b> ratings"
                    f"{f', {rating_reviews:,} reviews' if rating_reviews else ''})"
                )

        # Core metadata that should NOT be removed
        if isbn:
            parts.append(f"🆔 <b>ISBN:</b> <code>{isbn}</code>")
        if pages:
            parts.append(f"📄 <b>Pages:</b> {pages}")
        if year:
            parts.append(f"📅 <b>Year:</b> {year}")

        # Optional lower-priority fields
        if lang:
            parts.append(f"🌐 <b>Language:</b> {html_escape(lang)}")
        if publisher:
            parts.append(f"🏢 <b>Publisher:</b> {publisher}")

        parts.append("")  # blank line before summary

        # Description with expandable blockquote - will be truncated first if needed
        desc_text = (book.get("description") or "").strip()
        if desc_text:
            # Clean HTML tags
            desc_text = re.sub(r"<[^>]+>", "", desc_text)
            desc_text = re.sub(r"\s+", " ", desc_text).strip()
            # Escape for HTML
            desc_text = html_escape(desc_text)
            parts.append("📄 <b>Summary</b>")
            parts.append(f"<blockquote expandable>{desc_text}</blockquote>")

        parts.append("")
        parts.append("🔵 <b>Source:</b> Hardcover")

        # Join and ensure length is safe
        caption = "\n".join(parts)

        # Final safety check - if still too long, truncate description FIRST
        # (never remove core metadata: ISBN, Pages, Year)
        if len(caption) > 1020:
            # Find description parts
            for i, part in enumerate(parts):
                if part == "📄 <b>Summary</b>":
                    if i + 1 < len(parts) and parts[i + 1].startswith("<blockquote expandable>"):
                        current_desc = parts[i + 1][23:-13]
                        parts_without_desc = parts[:i+1] + [""] + parts[i+2:]
                        base_length = len("\n".join(parts_without_desc))
                        max_desc_len = 1020 - base_length - 3
                        if max_desc_len > 10:
                            if len(current_desc) > max_desc_len:
                                truncated = current_desc[:max_desc_len]
                                last_space = truncated.rfind(" ")
                                if last_space > max_desc_len * 0.8:
                                    truncated = truncated[:last_space]
                                parts[i + 1] = f"<blockquote expandable>{truncated}...</blockquote>"
                    break
            caption = "\n".join(parts)

        return caption

    def _build_goodreads_keyboard(self, book: dict) -> InlineKeyboardMarkup:
        """Build keyboard with Goodreads button for expanded view."""
        gr_url = build_goodreads_url(book)
        keyboard = [[InlineKeyboardButton("📚 Open Goodreads 🔗", url=gr_url)]]
        return InlineKeyboardMarkup(keyboard)

    # ── Run ───────────────────────────────────────────────────────────────────

    def run(self):
        """Start the bot with long polling."""
        logger.info("=" * 80)
        logger.info("🚀 Starting Multi-Source Book Bot")
        logger.info("=" * 80)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        # Telegram retains updates while long polling is disconnected. Discard
        # that backlog on startup so commands sent while the bot was down are
        # not unexpectedly executed after it recovers.
        self.app.run_polling(drop_pending_updates=True)


# ── Vercel singleton (survives warm starts) ─────────────────────────────────
from dotenv import load_dotenv
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN in .env file")

_bot_instance: GoodreadsBot | None = None


def get_bot() -> GoodreadsBot:
    """Get or create the global bot instance (singleton for warm starts)."""
    global _bot_instance
    if _bot_instance is None:
        _bot_instance = GoodreadsBot(TELEGRAM_BOT_TOKEN, webhook_mode=True)
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        loop.run_until_complete(_bot_instance.app.initialize())
        loop.run_until_complete(_bot_instance._configure_telegram_commands())
    return _bot_instance
