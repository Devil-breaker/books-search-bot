"""Shared utilities — logging, escaping, cover-image validation, constants."""

import logging
import hashlib
import struct
import threading
import requests as _requests

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Realistic browser headers ──────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
}

_http_session_local = threading.local()


def get_http_session():
    """Return a reusable Requests session scoped to the current worker thread.

    Search and metadata providers run in worker threads. Thread-local sessions
    reuse keep-alive connections without sharing mutable Session state across
    concurrent threads.
    """
    session = getattr(_http_session_local, "session", None)
    if session is None:
        session = _requests.Session()
        _http_session_local.session = session
    return session

# ── HTML / Markdown helpers ─────────────────────────────────────────────────────
from telegram import helpers


def md_escape(text: str) -> str:
    """Escape characters for Telegram MarkdownV2."""
    return helpers.escape_markdown(text, version=2)


def html_escape(text: str) -> str:
    """Escape characters for Telegram HTML parse mode."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ── Cover‑image validation ──────────────────────────────────────────────────────
# Google Books serves an "image not available" placeholder for catalog‑only
# records (volume IDs ending in "AACAAJ" — metadata with no scanned content).
# At the zoom level this bot downloads, that placeholder is a byte‑identical
# PNG of a fixed size, while genuine covers come back as JPEG — so it can be
# detected and rejected instead of being sent to the user as a real cover.
_GB_PLACEHOLDER_MD5 = {"a64fa89d7ebc97075c1d363fc5fea71f"}
_GB_PLACEHOLDER_PNG_SIZES = {(575, 750), (300, 391)}


def _png_dimensions(data: bytes):
    """Return (width, height) for PNG bytes, else None."""
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            return struct.unpack(">II", data[16:24])
        except Exception:
            return None
    return None


def is_placeholder_image(content: bytes) -> bool:
    """True if the bytes are Google Books' 'image not available' placeholder."""
    if not content or len(content) < 100:
        return True
    if hashlib.md5(content).hexdigest() in _GB_PLACEHOLDER_MD5:
        return True
    # The placeholder is a PNG at one of a few fixed sizes; real covers are JPEG.
    if _png_dimensions(content) in _GB_PLACEHOLDER_PNG_SIZES:
        return True
    return False


def is_unreliable_gb_cover(volume_id: str) -> bool:
    """Google Books volume IDs ending in 'AACAAJ' are metadata‑only records
    with no cover art — their image URLs resolve to the placeholder."""
    return bool(volume_id) and volume_id.endswith("AACAAJ")


# ── Auto-translation ──────────────────────────────────────────────────────────
# Translates non-English text to English using Google Translate's free API.
# Uses the same requests library already imported — no extra dependencies.
# Handles 429 rate limits with a single retry after a short delay.

def translate_to_english(text: str) -> str:
    """Translate *text* to English. Returns original on failure."""
    if not text or not text.strip():
        return text
    # Chunk long text to avoid URL length limits and API truncation.
    # ~500 chars is a safe chunk size; Google Translate returns incomplete
    # translations for very long strings.
    CHUNK_SIZE = 480
    if len(text) > CHUNK_SIZE:
        chunks = [text[i : i + CHUNK_SIZE] for i in range(0, len(text), CHUNK_SIZE)]
        translated_chunks = []
        for chunk in chunks:
            result = _translate_chunk(chunk)
            translated_chunks.append(result)
            if result != chunk:
                pass  # translated OK
        return "".join(translated_chunks)
    return _translate_chunk(text)


def _translate_chunk(text: str) -> str:
    """Translate a single short chunk. Internal — always called by the public fn."""
    for attempt in range(2):
        try:
            resp = get_http_session().get(
                "https://translate.googleapis.com/translate_a/single",
                params={"client": "gtx", "sl": "auto", "tl": "en", "dt": "t", "q": text},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=8,
            )
            if resp.status_code == 429:
                if attempt == 0:
                    import time

                    time.sleep(1.5)
                    continue
                logger.warning("Translation failed: status=429 (rate-limited)")
                return text
            if resp.status_code != 200:
                logger.warning(f"Translation failed: status={resp.status_code}")
                return text
            # Parse: [[["translated","original",...], ...], lang, ...]
            try:
                data = resp.json()
            except ValueError:
                logger.warning("Translation failed: non-JSON response")
                return text
            if not isinstance(data, list) or not data:
                logger.warning("Translation failed: unexpected response structure")
                return text
            sentences = data[0]
            if not isinstance(sentences, list):
                logger.warning("Translation failed: unexpected response structure [sentences]")
                return text
            parts = []
            for part in sentences:
                if isinstance(part, list) and len(part) > 0 and part[0]:
                    parts.append(part[0])
            translated = "".join(parts)
            return translated if translated else text
        except Exception as e:
            logger.warning(f"Translation failed: {e}")
            return text
    return text


# ── Language detection ───────────────────────────────────────────────────────────
# Used to skip translate_to_english() for descriptions already in English,
# avoiding unnecessary HTTP requests and 429 rate-limit hits.
#
# Strategy:
#   1. Strong non-Latin-script indicators → non-English (no API call needed)
#   2. Latin-only text → heuristic word-frequency check against common English words
#   3. Conservative for very short text (< 50 chars) to avoid false positives
#      that would break valid short English descriptions.


def is_english_description(text: str | None) -> bool:
    """Return True if *text* is detected as English (skip translation).

    Detection is fast and local (no HTTP calls). Falls back to False
    (translate) when uncertain, preserving existing safe behaviour.
    """
    if not text:
        return True  # empty → nothing to translate

    sample = text[:200]  # first 200 chars are enough for a reliable signal

    # ── Strong non-Latin-script signals ───────────────────────────────────────
    # Any of these character ranges in the text is a near-certain non-English
    # indicator; no English text uses Cyrillic, CJK, Arabic, etc.
    NON_LATIN_RANGES = (
        (0x0400, 0x04FF),   # Cyrillic
        (0x1100, 0x11FF),   # Hangul Jamo (Korean)
        (0x2E80, 0x2EFF),   # CJK Radicals Supplement
        (0x3000, 0x303F),   # CJK Symbols and Punctuation (also catches 々 〆)
        (0x3040, 0x309F),   # Hiragana (Japanese)
        (0x30A0, 0x30FF),   # Katakana (Japanese)
        (0x3400, 0x4DBF),   # CJK Unified Ideographs Extension A
        (0x4E00, 0x9FFF),   # CJK Unified Ideographs (Chinese)
        (0xAC00, 0xD7AF),   # Hangul Syllables (Korean)
        (0x0600, 0x06FF),   # Arabic
        (0x0590, 0x05FF),   # Hebrew
        (0x0E00, 0x0E7F),   # Thai
        (0x0900, 0x097F),   # Devanagari (Hindi, etc.)
        (0x0D80, 0x0DFF),   # Malayalam, Sinhala
        (0x1000, 0x109F),   # Burmese, Myanmar
        (0x1200, 0x137F),   # Ethiopic
        (0x1780, 0x17FF),   # Khmer
        (0x1950, 0x197F),   # Tibetan
        (0x1B00, 0x1B7F),   # Balinese
        (0x1F00, 0x1FFF),   # Greek
    )
    for char in sample:
        cp = ord(char)
        for lo, hi in NON_LATIN_RANGES:
            if lo <= cp <= hi:
                # Strong non-Latin signal; log the Unicode block name briefly
                _block = _unicode_block_name(cp)
                logger.info(f"Translation required: detected language=non-Latin ({_block})")
                return False

    # ── Short-description conservative path ─────────────────────────────────────
    # For very short text, character-script detection is unreliable; most short
    # English strings are purely ASCII letters/spaces/punctuation.
    if len(sample) < 30:
        if all(ord(c) < 128 or c in " .,!?-'\"/\n" for c in sample):
            logger.info("Translation skipped: description detected as English (ASCII-only)")
            return True
        logger.info("Translation language detection uncertain; description too short to analyse reliably")
        return False  # translate to be safe

    # ── Heuristic: English word frequency + accented-char guard ─────────────────
    # Count common English word hits relative to total words.
    # Also guard against accented European text (ü/ß/é/ñ/ô etc.) which is a
    # strong non-English signal even when common-word ratio passes.
    COMMON_ENGLISH_WORDS = frozenset({
        "the", "be", "to", "of", "and", "a", "in", "that", "have", "it",
        "for", "not", "on", "with", "he", "as", "you", "do", "at",
        "this", "but", "his", "by", "from", "they", "we", "say", "her", "she",
        "or", "an", "will", "my", "one", "all", "would", "there", "their", "what",
        "so", "up", "out", "if", "about", "who", "get", "which", "go", "me",
        "when", "make", "can", "like", "time", "no", "just", "him", "know", "take",
        "people", "into", "year", "your", "good", "some", "could", "them", "see",
        "other", "than", "then", "now", "look", "only", "come", "its", "over",
        "think", "also", "back", "after", "use", "two", "how", "our", "work",
        "first", "well", "way", "even", "new", "want", "because", "any", "these",
        "give", "day", "most", "us",
    })

    # Accented Latin characters common in European languages but rare in English.
    # English descriptions very rarely contain these; they are a reliable signal.
    _accents = "äöüßàâçéèêëîïôùûÿœæø"
    accent_count = sum(1 for c in sample.lower() if c in _accents)
    # Exclude ASCII from the denominator to avoid false signals from short mixed text
    non_ascii_chars = [c for c in sample if ord(c) >= 128]
    if non_ascii_chars:
        accent_ratio = accent_count / len(non_ascii_chars)
        if accent_ratio > 0.25:  # > 25 % accented chars among non-ASCII chars
            logger.info("Translation required: detected language=non-English (accented characters)")
            return False

    words = sample.lower().split()
    if not words:
        return True

    english_word_hits = sum(1 for w in words if w in COMMON_ENGLISH_WORDS)
    english_ratio = english_word_hits / len(words)

    if english_ratio >= 0.06:  # at least ~6 % common English words
        logger.info("Translation skipped: description detected as English")
        return True
    else:
        logger.info("Translation required: detected language=non-English")
        return False


def _unicode_block_name(cp: int) -> str:
    """Return a short human-readable name for a Unicode code point."""
    # Simple ranges for the blocks we check in is_english_description
    if 0x0400 <= cp <= 0x04FF:   return "Cyrillic"
    if 0x1100 <= cp <= 0x11FF:   return "Hangul Jamo"
    if 0x3000 <= cp <= 0x303F:   return "CJK Punctuation"
    if 0x3040 <= cp <= 0x309F:   return "Hiragana"
    if 0x30A0 <= cp <= 0x30FF:   return "Katakana"
    if 0x3400 <= cp <= 0x4DBF:   return "CJK Ext A"
    if 0x4E00 <= cp <= 0x9FFF:   return "CJK"
    if 0xAC00 <= cp <= 0xD7AF:   return "Hangul"
    if 0x0600 <= cp <= 0x06FF:   return "Arabic"
    if 0x0590 <= cp <= 0x05FF:   return "Hebrew"
    if 0x0E00 <= cp <= 0x0E7F:   return "Thai"
    if 0x0900 <= cp <= 0x097F:   return "Devanagari"
    if 0x0D80 <= cp <= 0x0DFF:   return "Malayalam/Sinhala"
    if 0x1000 <= cp <= 0x109F:   return "Burmese"
    if 0x1200 <= cp <= 0x137F:   return "Ethiopic"
    if 0x1780 <= cp <= 0x17FF:   return "Khmer"
    if 0x1950 <= cp <= 0x197F:   return "Tibetan"
    if 0x1B00 <= cp <= 0x1B7F:   return "Balinese"
    if 0x1F00 <= cp <= 0x1FFF:   return "Greek"
    if 0x2000 <= cp <= 0x2BFF:   return "Punctuation/Shapes"
    return "unknown"
