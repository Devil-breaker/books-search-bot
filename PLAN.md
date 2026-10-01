# Plan: Vercel Deployment for Multi-Source Books Search Bot

## Context

The bot is a Telegram bot (`python-telegram-bot` v21) that:
- Uses **polling** (`app.run_polling()`) — a persistent loop calling `getUpdates` every few seconds
- Scrapes Google Books, iTunes, Hardcover.app, StoryGraph, and Goodreads
- Has a `GoodreadsBot` class with command/button handlers
- Has a `MultiSourceBookAggregator` class for multi-source book search

**Problem**: Vercel is serverless — no persistent process = no long-running polling loop.
**Solution**: Convert to **webhook mode** (Telegram pushes updates to our endpoint) + **Vercel Cron Jobs** for periodic tasks (cache flush, keep-warm heartbeat).

## Files to Create / Modify

### 1. `src/bot.py` (NEW)
Refactored, all original bot logic preserved. Key changes:
- Add a `VercelBot` class that wraps `GoodreadsBot` but exposes a `process_update()` method
- Keep the original `GoodreadsBot` in `src/bot.py` (same logic as `goodreads_bot_advanced.py`, just extracted)
- `process_update(raw_update: dict) -> bool` — processes one Telegram update dict (from webhook payload)
- Global singleton `bot_instance` that survives Vercel warm starts

### 2. `api/webhook.py` (NEW)
Vercel API route — receives Telegram webhook POSTs:
```
POST /api/webhook
```
- Parse incoming JSON → `raw_update`
- Call `bot_instance.process_update(raw_update)`
- Return `{"ok": true}` or error
- Telegram expects a 200 response quickly; `process_update` is fast in sync mode

### 3. `api/cron.py` (NEW)
Vercel Cron route — called by Vercel's cron scheduler:
```
POST /api/cron
```
- Secured with `X-Vercel-Cron` header + optional `CRON_SECRET` env var
- Flushes `MultiSourceBookAggregator._hc_cache` (clears Hardcover API cache daily)
- Acts as keep-warm heartbeat (runs every 10 minutes to prevent cold starts)

### 4. `vercel.json` (NEW)
Vercel project configuration:
- **`crons`**: `POST /api/cron` every 10 minutes (max resolution)
- Routes: `api/` → serverless Python
- Headers: security headers for webhook endpoint

### 5. `vercel_requirements.txt` (NEW)
Same as `requirements.txt` — copied for Vercel deployment.

### 6. `goodreads_bot_advanced.py`
**Unchanged** — stays as-is for local/Docker deployment (polling mode).

### 7. `README.md`
Add a **"Deploy to Vercel"** section with:
- Prerequisites (Vercel account, Telegram Bot Token)
- `vercel login` / `vercel deploy` steps
- Registering the webhook URL with Telegram
- Environment variables on Vercel dashboard
- How cron keeps the bot warm

## Deployment Flow

```
User deploys to Vercel
  → Vercel creates the Python serverless functions
  → User sets env vars in Vercel dashboard (TELEGRAM_BOT_TOKEN, etc.)
  → User visits /api/cron once (or uses Telegram's setWebhook API)
  → Telegram starts sending updates to /api/webhook
  → Vercel Cron fires every 10 min → /api/cron → flushes cache + stays warm
```

## Security
- Webhook uses `X-Telegram-Bot-Api-Secret-Token` (optional, configurable)
- Cron uses `X-Vercel-Cron` header (auto-injected by Vercel)
- Optional `CRON_SECRET` env var for extra verification
- No external connections except to Telegram API, Google Books, etc.

## Verification
After deployment:
1. Check `/api/cron` returns `{"status": "ok", "cache_flush": true}`
2. Send `/start` to the bot on Telegram → welcome message appears
3. Send `/search Harry Potter` → results appear with inline buttons
4. Click a book → book details with cover image shown