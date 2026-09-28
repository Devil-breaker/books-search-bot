"""GoodreadsBot — all Telegram command and callback handlers."""

import asyncio
import copy
import hashlib
import json
import os
import re
import tempfile
import time
import requests
import unicodedata
from io import BytesIO
from PIL import Image
from urllib.parse import urlsplit

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InputMediaPhoto,
)
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, InlineQueryHandler, ContextTypes
from telegram.constants import ParseMode
from telegram.error import NetworkError, TimedOut

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
            .build()
        )
        # search_cache: {user_id: (books_list, timestamp)}
        # Entries expire after _SEARCH_CACHE_TTL seconds.
        self.search_cache: dict = {}
        self._SEARCH_CACHE_TTL: int = 60 * 60  # 60 minutes
        # Per-user current page and query text for normal search pagination
        self._search_page_cache: dict[int, int] = {}  # user_id -> page number
        self._search_query_cache: dict[int, str] = {}  # user_id -> query text
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
        # Suppress repeated cancellation follow-up messages per user.
        self._clarification_cancel_notice_rate_limit: dict[int, float] = {}
        # Escalating abuse controls for repeated clarification cancellations.
        self._clarification_cancel_abuse: dict[int, dict] = {}
        self._clarification_abuse_notice_rate_limit: dict[int, float] = {}
        owner_id = os.getenv("BOT_OWNER_ID", "").strip()
        try:
            self._owner_user_id: int | None = int(owner_id) if owner_id else None
        except ValueError:
            self._owner_user_id = None
            logger.warning("BOT_OWNER_ID must be a numeric Telegram user ID; owner exemption is disabled")
        if not owner_id:
            logger.warning("BOT_OWNER_ID is not configured; no user is exempt from cancellation abuse limits")
        self.setup_handlers()

    _CANCEL_ABUSE_THRESHOLD = 3
    _CANCEL_ABUSE_WINDOW_SECONDS = 60
    _CANCEL_ABUSE_COOLDOWN_SECONDS = 5 * 60
    _CANCEL_ABUSE_ESCALATION_WINDOW_SECONDS = 24 * 60 * 60
    _CANCEL_ABUSE_BLOCK_SECONDS = 60 * 60

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

    def _record_clarification_cancel(self, user_id: int) -> str | None:
        """Count cancel cycles and return an escalation action when a limit is reached."""
        if self._owner_user_id is not None and user_id == self._owner_user_id:
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
        if self._owner_user_id is not None and user_id == self._owner_user_id:
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
        title_hint = " ".join(tokens[:last_meaningful_idx])
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
            params={"q": gb_query, "maxResults": 6, "langRestrict": "en"},
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
            if title_score >= 0.5 and author_score >= 0.5:
                return {"title": vol_title, "author": vol_author, "source": "google_books"}
        return None

    async def _try_clarification(
        self, update: Update, query: str
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

        def do_discover():
            return self._discover_candidate(query, title_hint, author_hint)

        discovery_started = time.perf_counter()
        candidate = await asyncio.to_thread(do_discover)
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
        prompt_text = (
            f"Did you mean {html_escape(canonical_title)} "
            f"by {html_escape(canonical_author)}?"
        )
        if update.effective_chat.type != "private":
            prompt_msg = await update.message.reply_text(
                prompt_text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                reply_to_message_id=message_id,
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
        user_id = update.effective_user.id
        entry = entry or self._clarification.get(user_id, {})
        chat_id = entry.get("chat_id", update.effective_chat.id)
        reply_to_id = (
            entry.get("source_message_id")
            if entry.get("chat_type", update.effective_chat.type) != "private"
            else None
        )
        bot = self.app.bot

        async def send_result(text: str, reply_markup=None) -> None:
            kwargs = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": ParseMode.HTML,
                "reply_markup": reply_markup,
            }
            if reply_to_id:
                kwargs["reply_to_message_id"] = reply_to_id
            try:
                await bot.send_message(**kwargs)
            except Exception:
                # A group may have deleted the source message while the search ran.
                if "reply_to_message_id" not in kwargs:
                    raise
                kwargs.pop("reply_to_message_id", None)
                await bot.send_message(**kwargs)

        try:
            books = await self._aggregate_search_results(search_q, limit=10)
            books = self._rank_search_results(
                self._deduplicate_search_results(books, search_q), search_q
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
            await send_result(results_text, reply_markup=keyboard)
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

    @staticmethod
    def _author_hint_is_complete(author_hint: str | None, candidate_author: str | None) -> bool:
        """Check whether a supplied author is already as specific as the match."""
        hint_tokens = re.findall(r"[a-z0-9]+", (author_hint or "").casefold())
        candidate_tokens = re.findall(r"[a-z0-9]+", (candidate_author or "").casefold())
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

        def fetch_items(search_query: str, label: str, max_results: int) -> list:
            try:
                params = {"q": search_query, "maxResults": max_results, "langRestrict": "en"}
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
                    token for token in re.findall(r"[a-z0-9]+", author_hint_norm)
                    if token not in self._STOPWORDS and len(token) >= 2
                ]
                candidate_title_tokens = set(re.findall(r"[a-z0-9]+", vol_title_norm))
                author_is_independently_identified = (
                    " by " in query.lower()
                    or not author_hint_tokens
                    or any(token not in candidate_title_tokens for token in author_hint_tokens)
                )
                author_score = (
                    self._score_author_hint(author_hint_norm, vol_author_norm)
                    if author_hint_norm and author_is_independently_identified else 0.0
                )
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

        # Fallback: broader split query when structured search found nothing
        if title_hint and author_hint:
            fallback_query = f"{title_hint} {author_hint}"
            fb_items = fetch_items(fallback_query, "fallback", 6)
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
        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CommandHandler("help", self.help_command))
        self.app.add_handler(CommandHandler("search", self.search_command))
        self.app.add_handler(CommandHandler("ping", self.ping_command))
        self.app.add_handler(CallbackQueryHandler(self.button_callback))
        self.app.add_handler(InlineQueryHandler(self.inline_search))

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

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send welcome message on /start."""
        await update.message.reply_text( f"""
🤖 <b>Multi-Source Book Bot</b>

Welcome! I search across multiple sources to find the best book information,
covers, and descriptions.

<b>How to use:</b>
• <code>/search &lt;book_title&gt;</code> - Search for books
• <code>@{context.bot.username} &lt;book_name&gt;</code> - Inline search from any chat

<b>Example:</b>
<code>/search Harry Potter and the Prisoner of Azkaban</code>
<code>@{context.bot.username} Harry Potter</code>

<b>Data Sources:</b>
📚 Google Books - Descriptions & metadata
🍎 iTunes - High-resolution covers
💠 Hardcover.app - Community ratings
📖 StoryGraph - Social reading ratings

Use /help for more information.
            """ .strip(),
            parse_mode=ParseMode.HTML,
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send help text on /help."""
        await update.message.reply_text( f"""
<b>📚 Multi-Source Book Bot Help</b>

<b>Commands:</b>
<code>/start</code> - Show welcome message
<code>/help</code> - Show this help message
<code>/search &lt;query&gt;</code> - Search for books
<code>/ping</code> - Check if the bot is running

<b>Features:</b>
✓ Searches multiple sources simultaneously
✓ Combines best data from each source
✓ High-resolution covers from iTunes
✓ Descriptions from Google Books
✓ Community ratings from Hardcover.app
✓ Social ratings from StoryGraph
✓ Download covers as image files

<b>Inline Search:</b>
Use the bot from any Telegram chat by typing:
<code>@{context.bot.username} &lt;book_name&gt;</code>

Example: <code>@{context.bot.username} Harry Potter</code>

<b>Tips:</b>
• Use full book titles for best results
• Include author name for better matching
• Try different keywords if no results
            """ .strip(),
            parse_mode=ParseMode.HTML,
        )

    async def ping_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Reply with bot status on /ping."""
        await update.message.reply_text("✅ Bot is running and polling Telegram!")

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

        tokens = cls._dedup_tokens(raw_title)
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

    def _merge_duplicate_book_data(self, target: dict, other: dict) -> None:
        """Merge duplicates while preferring the more informative record."""
        target_title = target.get("title", "") or ""
        other_title = other.get("title", "") or ""
        if len(self._dedup_tokens(other_title)) < len(self._dedup_tokens(target_title)):
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
            elif key == "description" and value and current:
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

    def _deduplicate_search_results(self, books: list[dict],
                                     query: str) -> list[dict]:
        """Merge duplicate work records using ISBN, normalized title and author evidence.

        Edition/subtitle text is retained in the title key. Unknown-author records
        join a known-author cluster only when that title has a single unambiguous
        author cluster in the result set.
        """
        records = [dict(book) for book in (books or [])]
        count = len(records)
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

        clusters: dict[int, dict] = {}
        for index, book in enumerate(records):
            root = find(index)
            if root not in clusters:
                clusters[root] = dict(book)
            else:
                self._merge_duplicate_book_data(clusters[root], book)

        merged = list(clusters.values())
        removed = count - len(merged)
        if removed:
            logger.info("Removed %s duplicate title/author results for query=%r", removed, query)
        return merged

    def _rank_search_results(self, books: list[dict], query: str) -> list[dict]:
        """Stable relevance ordering using query token coverage in title and author."""
        query_tokens = [token for token in self._dedup_tokens(query)
                        if token not in self._STOPWORDS]
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

        return sorted(books, key=score, reverse=True)

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

        # Inline mode is another search entry point; apply the same user cooldown.
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

        final = merged[:20]

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

        categories = book.get("categories", [])
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
        """Build inline keyboard for photo results with hourglass button."""
        keyboard = [[InlineKeyboardButton("⏳", callback_data=f"hourglass_{callback_key}")]]
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
        categories = book.get("categories", [])
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
        """Download cover image and save to a temp file. Returns path or None.

        Args:
            cover_url: URL to download.
            book: Book dict (optional). Used for cover fallback and diagnostics.
                  When the primary cover is a placeholder, retries with the
                  Hardcover cover from book["_hardcover_match"]["cover_url"] if available.
        """
        cover_source = (book or {}).get("cover_source", "unknown")

        def _log_cover_diagnostic(source, url, status, ctype, clen, final_url, width, height):
            """Log diagnostic info for a cover download (PART 4)."""
            domain = urlsplit(url).netloc
            final_domain = urlsplit(final_url).netloc if final_url != url else domain
            logger.info(
                f"Cover diag: source={source} url={url[:70]} "
                f"status={status} type={ctype} len={clen} "
                f"domain={final_domain} dims={width}x{height}"
            )

        def _download_one(url: str, source: str = cover_source):
            """Attempt one cover download. Returns (bytes, status, ctype, clen, final_url, width, height)."""
            response = get_http_session().get(url, headers=HEADERS, timeout=15, allow_redirects=True)
            response.raise_for_status()
            final_url = response.url
            ctype = response.headers.get("Content-Type", "")
            clen = len(response.content)
            width = height = None
            try:
                img = Image.open(BytesIO(response.content))
                width, height = img.size
            except Exception:
                pass
            _log_cover_diagnostic(source, url, response.status_code, ctype, clen, final_url, width, height)
            return response.content, response.status_code, ctype, clen, final_url, width, height

        try:
            title = (book or {}).get("title", "")
            result_source = cover_source
            logger.info(f"📥 Downloading cover: {cover_url[:60]}... source={cover_source} title={title}")
            content, status, ctype, clen, final_url, width, height = _download_one(cover_url)

            if is_placeholder_image(content):
                logger.warning(f"⚠️ Cover placeholder detected: source={cover_source} title={title}")
                # ── PART 6 fallback: try Hardcover cover if available ─────────
                if book and cover_source == "google_books":
                    hc_cover = book.get("_hardcover_match", {}).get("cover_url")
                    if hc_cover:
                        logger.info(f"Cover fallback: source=hardcover title={title}")
                        try:
                            content, status, ctype, clen, final_url, width, height = (
                                _download_one(hc_cover, source="hardcover")
                            )
                            if is_placeholder_image(content):
                                logger.warning(
                                    f"⚠️ Fallback cover also placeholder; skipping. title={title}"
                                )
                                return None
                            logger.info(f"✅ Cover fallback OK: source=hardcover title={title} bytes={clen}")
                            # Fall through — content now holds the valid fallback bytes.
                            result_source = "hardcover"
                        except Exception as e:
                            logger.warning(f"Fallback cover download failed: {e} title={title}")
                            return None
                    else:
                        logger.info(f"No Hardcover cover available for fallback. title={title}")
                        return None
                else:
                    return None

            # ── Write temp file (reached by both primary and fallback paths) ──────
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            temp_file.write(content)
            temp_file.close()
            logger.info(f"✅ Downloaded: source={result_source} title={title} bytes={clen}")
            return temp_file.name
        except Exception as e:
            logger.error(f"Error downloading image: {e}")
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
        author = html_escape(book.get("author", "Unknown"))
        rating = html_escape(str(book.get("rating_formatted", book.get("rating", "N/A"))))
        rating_cnt = book.get("rating_count", 0)
        isbn = html_escape(book.get("isbn", ""))
        pages = str(book.get("page_count", 0))
        year = book.get("published_date", "")[:4]
        desc = (book.get("description") or "").strip()

        # Clean HTML tags from description
        desc = re.sub(r"<[^>]+>", "", desc)
        desc = re.sub(r"\s+", " ", desc).strip()

        # Truncate to ~800 chars at a word boundary
        if len(desc) > 800:
            cutoff = desc.rfind(" ", 0, 800)
            if cutoff > 100:
                desc = desc[:cutoff] + "..."
            else:
                desc = desc[:800] + "..."

        desc_escaped = html_escape(desc)

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

        parts = []

        # Header
        parts.append(f"<b>{title}</b>")
        parts.append(f"<i>{author}</i>")

        # Rating line with stars
        if rating_cnt > 0:
            try:
                rating_for_float = str(book.get("rating", "")).replace(",", ".")
                rating_num = float(rating_for_float)
                stars = "⭐" * min(int(rating_num), 5)
                parts.append(f"{stars} <b>{rating}</b>/5 (<b>{rating_cnt:,} ratings</b>)")
            except ValueError:
                parts.append(f"<b>{rating}</b>/5 (<b>{rating_cnt:,} ratings</b>)")
        else:
            parts.append("❓ <b>No ratings yet</b>")

        parts.append("")  # blank line

        # Metadata
        if isbn:
            parts.append(f"📖 <b>ISBN:</b> <code>{isbn}</code>")
        if pages and int(pages) > 0:
            parts.append(f"📄 <b>Pages:</b> <code>{pages}</code>")
        if year:
            parts.append(f"📅 <b>Year:</b> <code>{year}</code>")

        # Genres
        categories = book.get("categories", [])
        if categories:
            genres_str = ", ".join(categories[:5])
            parts.append(f"🏷️ <b>Genres:</b> {html_escape(genres_str)}")

        # Description
        if desc_escaped:
            parts.append("")
            parts.append(desc_escaped)

        # Footer
        parts.append("")
        parts.append(f"{source_emoji} <i>Source: {html_escape(source_text)}</i>")

        # Links
        if book.get("info_link"):
            parts.append(f'<a href="{html_escape(book["info_link"])}">📚 More Info</a>')
        if book.get("goodreads_url"):
            parts.append(f'<a href="{html_escape(book["goodreads_url"])}">Goodreads Page</a>')

        return "\n".join(parts)

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

    # ── Search ────────────────────────────────────────────────────────────────
    async def search_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /search command."""
        try:
            if not context.args:
                await update.message.reply_text(
                    "Please provide a book title or author name.\n\n"
                    "Example: <code>/search Harry Potter</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            query_text = " ".join(context.args)

            if not query_text or len(query_text.strip()) < 2:
                await update.message.reply_text(
                    "Please provide a valid search query (at least 2 characters).\n\n"
                    "Example: <code>/search Harry Potter</code>",
                    parse_mode=ParseMode.HTML,
                )
                return

            await update.message.chat.send_action("typing")
            logger.info(f"👤 User search: {query_text}")

            if await self._reject_search_during_clarification_restriction(update):
                return

            # Check if clarification is needed BEFORE calling the aggregator.
            stop = await self._try_clarification(update, query_text)
            if stop:
                return

            # Get aggregator results, then preload Hardcover ratings for visible books.
            # Hardcover is the single source of truth for list ratings; each book on the
            # current page is looked up individually via _get_hardcover_cached (cached, concurrent).
            books = await self._aggregate_search_results(query_text, limit=10)
            books = self._rank_search_results(
                self._deduplicate_search_results(books, query_text), query_text
            )

            if not books:
                logger.warning(f"No books found for: {query_text}")
                await update.message.reply_text(
                    f"❌ <b>No books found</b> for '<b>{html_escape(query_text)}</b>'\n\n"
                    "<i>Try different keywords or check spelling.</i>",
                    parse_mode=ParseMode.HTML,
                )
                return

            user_id = update.effective_user.id
            self._set_cached_books(user_id, books)
            self._search_page_cache[user_id] = 1
            self._search_query_cache[user_id] = query_text

            # Preload Hardcover ratings for page 1 BEFORE building the UI.
            # Hardcover must be available so ratings appear in the result list.
            # _preload_hardcover_ratings_for_page is async; internally it runs concurrent
            # Hardcover lookups via asyncio.gather() inside asyncio.to_thread().
            await self._preload_hardcover_ratings_for_page(books, 1, 5)
            logger.info("Normal search result list building AFTER Hardcover preload")
            results_text, keyboard = self._build_search_results_message(
                books, query_text, user_id, 1, 5
            )
            await update.message.reply_text(results_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)

        except Exception as e:
            logger.error(f"Error in search_command: {e}", exc_info=True)
            await update.message.reply_text(
                "❌ An error occurred while searching.", parse_mode=ParseMode.HTML
            )

    # ── Button callbacks ────────────────────────────────────────────────────────

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle all inline button presses."""
        try:
            query = update.callback_query
            callback_data = query.data

            # ── Pagination ───────────────────────────────────────────────────
            if callback_data.startswith("page_"):
                parts = callback_data.split("_")
                user_id = int(parts[1])
                page_num = int(parts[2])

                books = self._get_cached_books(user_id)
                if books is None:
                    await query.answer("Search results expired.", show_alert=True)
                    return

                self._search_page_cache[user_id] = page_num
                query_text = self._search_query_cache.get(user_id, "")

                # Preload Hardcover ratings for this page BEFORE rebuilding the message.
                # Hardcover must be available so ratings appear in the list.
                # _preload_hardcover_ratings_for_page is async; internally it runs concurrent
                # Hardcover lookups via asyncio.gather() inside asyncio.to_thread().
                await self._preload_hardcover_ratings_for_page(books, page_num, 5)
                logger.info(f"Building result list page {page_num} AFTER Hardcover preload")
                results_text, keyboard = self._build_search_results_message(
                    books, query_text, user_id, page_num, 5
                )
                await query.edit_message_text(
                    text=results_text, reply_markup=keyboard, parse_mode=ParseMode.HTML
                )
                return

            # ── Back to results ────────────────────────────────────────────────
            if callback_data.startswith("back_"):
                parts = callback_data.split("_")
                user_id = int(parts[1])

                books = self._get_cached_books(user_id)
                if books is None:
                    await query.answer("Search results expired.", show_alert=True)
                    return

                page_num = self._search_page_cache.get(user_id, 1)
                query_text = self._search_query_cache.get(user_id, "")
                results_text, keyboard = self._build_search_results_message(
                    books, query_text, user_id, page_num=page_num, page_size=5
                )

                # Delete the current message (could be a photo or text) and send
                # a clean, fresh text-only message with the results list.
                await query.delete_message()
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=results_text,
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML,
                )
                return

            # ── Download cover ────────────────────────────────────────────────
            if callback_data.startswith("download_"):
                parts = callback_data.split("_")
                user_id = int(parts[1])
                book_idx = int(parts[2])

                books = self._get_cached_books(user_id)
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

            # ── Hourglass button (inline details expansion) ───────────────────
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

                # Build expanded caption
                expanded_caption = self._build_expanded_inline_caption(book_data)

                # Build Goodreads keyboard
                gr_keyboard = self._build_goodreads_keyboard(book_data)

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
                            reply_markup=gr_keyboard,
                        )
                        logger.info(
                            f"Inline details expanded (photo) for: {book_data.get('title', 'Unknown')}"
                        )
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

                self._clarification.pop(user_id, None)
                try:
                    await query.delete_message()
                except Exception as exc:
                    # Continue the selected action if the prompt was already removed.
                    logger.info("Clarification prompt could not be deleted: %s", exc)

                if action == "clar_cancel":
                    abuse_action = self._record_clarification_cancel(user_id)
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

            books = self._get_cached_books(user_id)
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

            # Translate description lazily when user selects a book (performance fix).
            # Skip if description is already English to avoid unnecessary HTTP calls.
            if book.get("description") and not is_english_description(book["description"]):
                book["description"] = await asyncio.to_thread(
                    translate_to_english, book["description"]
                )

            text_info = self.format_book_message(book)

            # Truncate caption to stay within Telegram's 1024 character limit.
            #
            # The caption is HTML, so we can't just slice at a fixed byte offset —
            # truncating mid-<a ...> tag leaves broken markup that Telegram rejects
            # ("Can't parse entities: unsupported start tag ..."). Any text-derived
            # HTML tags (the <a href> links) may sit near the cutoff point, so:
            # 1. Strip tags to get clean plain text
            # 2. Truncate at a word boundary
            # 3. Re-append the Goodreads link (exact, valid HTML) last
            MAX_CAPTION = 950
            if len(text_info) > MAX_CAPTION:
                # Truncate HTML safely without breaking markup or collapsing lines.
                # - Strip tags to "" (the message's real "\n" line breaks stay)
                # - Replace any HTML-coded newlines with plain newlines
                # - Collapse spaces around newlines, but keep the newlines
                plain = re.sub(r"<br\s*/?>", "\n", text_info, flags=re.I)
                plain = re.sub(r"<[^>]+>", "", plain)
                plain = re.sub(r" *\n *", "\n", plain).strip()
                cutoff = plain.rfind(" ", 0, MAX_CAPTION)
                gr_url = build_goodreads_url(book)
                suffix = f'\n\n<a href="{gr_url}">📖 View on Goodreads</a>'
                if cutoff > 0:
                    text_info = plain[:cutoff] + "..." + suffix
                else:
                    text_info = plain[:MAX_CAPTION] + "..." + suffix

            await query.delete_message()

            temp_file = None
            cover_url = book.get("cover_url")

            if cover_url:
                temp_file = await asyncio.to_thread(self.download_and_save_image, cover_url, book)

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
                    keyboard = [
                        [
                            InlineKeyboardButton(
                                "📥 Download Cover",
                                callback_data=f"download_{user_id}_{book_idx}",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "🔙 Back to Results",
                                callback_data=f"back_{user_id}",
                            )
                        ],
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)

                    logger.info(f"ABOUT TO SEND COVER: path={_fpath} size={_fsize} position={_fpos_after_open}")
                    with open(temp_file, "rb") as f:
                        await context.bot.send_photo(
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
                    keyboard = [
                        [
                            InlineKeyboardButton(
                                "🔙 Back to Results",
                                callback_data=f"back_{user_id}",
                            )
                        ]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await context.bot.send_message(
                        chat_id=query.message.chat_id,
                        text=text_info,
                        parse_mode=ParseMode.HTML,
                        reply_markup=reply_markup,
                    )
                finally:
                    self.cleanup_temp_file(temp_file)
            else:
                keyboard = [
                    [
                        InlineKeyboardButton(
                            "🔙 Back to Results",
                            callback_data=f"back_{user_id}",
                        )
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=text_info,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
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

        # Genres: limit to 5, remove duplicates
        categories = book.get("categories", [])
        if categories:
            # Remove duplicates while preserving order
            seen = set()
            unique_categories = []
            for cat in categories:
                if cat not in seen:
                    seen.add(cat)
                    unique_categories.append(cat)
            # Limit to 5
            limited_categories = unique_categories[:5]
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
    return _bot_instance
