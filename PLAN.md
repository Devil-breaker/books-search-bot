# Plan: Improve search clarification flow

## Changes

### 1. Fix cooldown fall-through (`_try_clarification`, ~line 303)

**Problem:** When the per-user, per-query cooldown is active, `_try_clarification()` sends the "please wait" notice then returns `False`, causing callers to continue with normal search.

**Fix:** Return `True` when rate-limited, so every caller stops immediately.

```
old:
    await update.message.reply_text(...)
    return False  ← allows normal search to run

new:
    await update.message.reply_text(...)
    return True  ← every caller stops
```

The callers (`handle_text`, `search_command`, `_search_execute`) all return immediately when `_try_clarification` returns `True`, so this closes the loop cleanly.

### 2. Add normalization helper (`_normalize_for_matching`)

New static method on `GoodreadsBot`:

```python
@staticmethod
def _normalize_for_matching(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace for word-level comparison."""
    # Lowercase, remove punctuation, collapse whitespace
    text = text.lower()
    text = re.sub(r"[.,;:!?'\"()\[\]—–-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text
```

### 3. Rewrite `_discover_candidate`

#### 3a. Accept explicit title/author hints

Change signature to:

```python
def _discover_candidate(self, query: str, title_hint: str | None = None,
                         author_hint: str | None = None) -> dict | None:
```

When `title_hint` and `author_hint` are both provided (from the "by" path), use them directly with a structured `intitle:` + `inauthor:` query.
When both are `None` (non-"by" query), generate plausible splits.

#### 3b. Generate plausible title/author splits (non-"by" only)

Split strategy for "harry potter rowling" / "confessions minato":

```
Query: "harry potter rowling" (no hints)
→ Generate splits:
    (title="harry potter",     author="rowling")        ← 1-1 split
    (title="harry",            author="potter rowling") ← 1-2 split
    (title="harry potter rowling", author="")           ← rejected (no author)
Stop at 2 title words. Require ≥1 meaningful author word.
```

```
Query: "confessions minato" (no hints)
→ Generate splits:
    (title="confessions", author="minato")              ← 1-1 split
    (title="confessions minato", author="")            ← rejected
```

#### 3c. Structured Google Books search

For each split (or for explicit hints), build and execute:

```
intitle:"{title_hint}" inauthor:"{author_hint}"
```

Use the existing `requests.get` call (replicate `search_google_books` URL pattern) to get up to 5 results.

#### 3d. Score candidates

For each candidate returned by Google Books:

```
title_score  = fraction of hint title's meaningful words found in candidate title
               (hint: "harry potter" → 2 words; candidate has them both → 2/2 = 1.0)
               Meaningful words = tokens > 2 chars, excluding stopwords

author_score = 1.0 if hint author token is contained in normalized candidate author
               Handles: "rowling" in "j.k. rowling"
                         "kana" in "kana e minato" (reversed name order)
                         surname-only matches full name
               = 0.0 if no match

joint_score  = title_score * author_score  (requires BOTH fields to match)
```

**Minimum thresholds:** Require `title_score >= 0.5` AND `author_score >= 0.5`.

#### 3e. Confidence / disambiguation

- Run all plausible splits through the scoring
- Collect all scored candidates
- Sort by `joint_score` descending
- **Accept** the top candidate only if:
  1. Both `title_score >= 0.5` AND `author_score >= 0.5`
  2. Top score ≥ 1.5× the second-best score (or there is no second candidate)
- Otherwise return `None` (skip clarification, continue normal search)

#### 3f. Return format (unchanged)

```python
return {
    "title": matched_cand_title,
    "author": matched_cand_author,
    "expanded_query": f"{title} by {author}",
}
```

### 4. Update `_try_clarification`

For "by" queries: pass `title_hint, author_hint` to `_discover_candidate`.
For non-"by" queries: pass `None, None` and let `_discover_candidate` generate splits.

### 5. Tests (`tests/test_clarification.py`)

| Test | What it verifies |
|---|---|
| `test_title_by_author_matches` | "confessions by kanae minato" triggers clarification |
| `test_non_by_harry_potter_rowling_matches_jk_rowling` | "harry potter rowling" → best candidate has title "Harry Potter..." and author "J.K. Rowling" |
| `test_non_by_confessions_minato_matches` | "confessions minato" → best candidate matches appropriate title+author |
| `test_title_only_not_triggered` | "Harry Potter" (no "by", short author) does NOT trigger clarification |
| `test_weak_match_no_clarification` | Low-scoring candidate (below threshold or ambiguous) → no clarification |
| `test_cooldown_same_query_returns_true` | Same-query retry during cooldown: `_try_clarification` returns `True` |
| `test_cooldown_different_query_proceeds` | Different query during cooldown: cooldown key differs → clarification proceeds |

All tests use `patch("requests.get")` to mock the Google Books HTTP response, returning structured JSON matching the API's volume format.

## Files modified
- `src/handlers.py` — `_try_clarification`, `_discover_candidate`, new `_normalize_for_matching`
- `tests/test_clarification.py` — new file

## Files not modified (scope guard)
- `tests/conftest.py`, `tests/test_aggregator.py`, `tests/test_utils.py`, `tests/test_search_ui.py`
- `src/aggregator.py`, `src/search.py`, `src/utils.py`