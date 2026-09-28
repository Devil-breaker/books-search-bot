"""Goodreads scraping — JSON‑LD primary, HTML fallback, rate‑limited."""

import time
import random
import re
import json
import requests
from bs4 import BeautifulSoup

from src.utils import get_http_session, logger

# ── Rate limiting ────────────────────────────────────────────────────────────────
MIN_REQUEST_INTERVAL = 2.0   # seconds between any two requests
JITTER_RANGE = (0.5, 1.5)     # extra random delay added each request

_GR_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.google.com/",
}
_last_request_ts = 0.0   # module‑level timestamp for rate‑limit


def _polite_get(url: str):
    """GET with rate‑buffer + jitter; returns Response or None on failure."""
    global _last_request_ts
    now = time.time()
    elapsed = now - _last_request_ts
    wait = max(0.0, MIN_REQUEST_INTERVAL - elapsed) + random.uniform(*JITTER_RANGE)
    if wait > 0:
        time.sleep(wait)

    try:
        resp = get_http_session().get(url, headers=_GR_HEADERS, timeout=12)
        _last_request_ts = time.time()
        if resp.status_code == 200:
            return resp
        # 429 or 503 → back off a bit more and retry once
        if resp.status_code in (429, 503, 502, 504):
            time.sleep(5.0 + random.uniform(0, 2))
            resp2 = get_http_session().get(url, headers=_GR_HEADERS, timeout=12)
            _last_request_ts = time.time()
            if resp2.status_code == 200:
                return resp2
    except Exception:
        pass
    return None


def _extract_json_ld(soup: BeautifulSoup) -> dict:
    """Pull structured data from the first JSON‑LD block on the page."""
    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        # Goodreads uses @type "Book"
        if isinstance(data, dict) and data.get("@type") == "Book":
            return data
        # Sometimes it's a list
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("@type") == "Book":
                    return item
    return {}


def _parse_json_ld(data: dict) -> dict:
    """Map JSON‑LD fields to the bot's internal dict."""
    out = {}
    out["title"] = data.get("name", "").strip()
    # Authors may be a dict or list of dicts
    authors = data.get("author")
    if isinstance(authors, list):
        out["author"] = ", ".join(
            a.get("name", "").strip() for a in authors if isinstance(a, dict)
        )
    elif isinstance(authors, dict):
        out["author"] = authors.get("name", "").strip()
    # Rating
    agg = data.get("aggregateRating")
    if isinstance(agg, dict):
        out["rating"] = float(agg.get("ratingValue", 0))
        out["rating_count"] = int(agg.get("ratingCount", 0))
    # Description
    out["description"] = (data.get("description") or "").strip()
    # ISBN – useful for cover fallback
    out["isbn"] = data.get("isbn", "")
    # Publication date
    out["published_date"] = data.get("datePublished", "")
    # Number of pages
    out["page_count"] = data.get("numberOfPages", 0)
    return out


def _html_fallback(soup: BeautifulSoup, isbn: str) -> dict:
    """Classic HTML scraping when JSON‑LD is missing/incomplete."""
    out = {"isbn": isbn}
    # Title & author – Goodreads uses specific classes / itemprop
    title_tag = soup.find("h1", {"data-testid": "bookTitle"})
    if title_tag:
        out["title"] = title_tag.get_text(strip=True)
    author_tag = soup.find("span", {"data-testid": "name"})
    if author_tag:
        out["author"] = author_tag.get_text(strip=True)

    # Rating
    rating_tag = soup.find("div", {"data-testid": "ratingValue"})
    if rating_tag:
        try:
            out["rating"] = float(rating_tag.get_text(strip=True))
        except ValueError:
            pass
    count_tag = soup.find("div", {"data-testid": "ratingCount"})
    if count_tag:
        m = re.search(r"[\d,]+", count_tag.get_text())
        if m:
            out["rating_count"] = int(m.group().replace(",", ""))

    # Description – often hidden behind a spoiler; take the first <div data-testid="description">
    desc_tag = soup.find("div", {"data-testid": "description"})
    if desc_tag:
        out["description"] = desc_tag.get_text(separator=" ", strip=True)

    # Cover – try to get the largest image from the page
    img_tag = soup.find("img", {"data-testid": "bookCover"})
    if img_tag and img_tag.get("src"):
        out["cover_url"] = img_tag["src"].replace("._SY160_", "").replace("._SY475_", "")
    # If still missing, try to construct from ISBN via Goodreads image service
    if not out.get("cover_url") and out.get("isbn"):
        out["cover_url"] = f"https://images.gr-assets.com/books/1405398843l/{out['isbn']}.jpg"
    return out


def scrape_goodreads(book_title: str, author: str = None) -> dict | None:
    """
    Given a title (and optional author) returns a dict with the fields the bot
    expects, or None if scraping failed.
    """
    query = f"{book_title} {author or ''}".strip()
    search_url = f"https://www.goodreads.com/search?q={requests.utils.quote(query)}"
    resp = _polite_get(search_url)
    if not resp:
        return None
    soup = BeautifulSoup(resp.text, "html.parser")

    # Grab the first result link
    first_result = soup.find("a", {"class": "bookTitle"})
    if not first_result or not first_result.get("href"):
        return None
    book_url = "https://www.goodreads.com" + first_result["href"]

    # Fetch the actual book page
    book_resp = _polite_get(book_url)
    if not book_resp:
        return None
    book_soup = BeautifulSoup(book_resp.text, "html.parser")

    # 1) Try JSON‑LD first
    json_ld = _extract_json_ld(book_soup)
    if json_ld:
        data = _parse_json_ld(json_ld)
        if data.get("title") and data.get("author"):
            return data

    # 2) Fallback to HTML selectors
    isbn = json_ld.get("isbn", "")
    data = _html_fallback(book_soup, isbn)
    if data.get("title") and data.get("author"):
        return data
    return None


def build_goodreads_url(book: dict) -> str:
    """
    Return the best Goodreads URL for a given book dict.

    Priority:
    1. Existing `goodreads_url` field (set by Goodreads scraping fallback)
    2. ISBN-based direct link  → https://www.goodreads.com/book/isbn/{isbn}
    3. Title + author search   → https://www.goodreads.com/search?q={title}+{author}
    """
    # 1) Already have a direct Goodreads URL (e.g. from scrape_goodreads fallback)
    if book.get("goodreads_url"):
        return book["goodreads_url"]

    # 2) ISBN-based direct link (preferred — goes to the exact book page)
    isbn = (book.get("isbn") or "").replace("-", "").strip()
    if isbn:
        return f"https://www.goodreads.com/book/isbn/{isbn}"

    # 3) Search by title + author
    title = book.get("title", "")
    author = book.get("author", "")
    search_query = f"{title} {author}".strip()
    return f"https://www.goodreads.com/search?q={requests.utils.quote(search_query)}"
