# 📚 Multi-Source Books Search Bot

A Telegram bot that searches for books across multiple sources (Google Books, Hardcover.app, OpenLibrary) and delivers rich results including covers, ratings, descriptions, and metadata — all in your chat.

## 🏗️ Architecture

The bot uses a **modular structure** with a single source of truth in `src/handlers.py`. All entry points (polling, Docker, or Vercel webhook) import from the same shared code, so fixes apply everywhere automatically.

```
src/
├── handlers.py    # GoodreadsBot class — bot logic, commands, UI (single source of truth)
├── aggregator.py  # MultiSourceBookAggregator — Google Books, Hardcover, OpenLibrary
├── search.py      # Search helpers, Goodreads URL builder
└── utils.py       # Logger, HTTP headers, HTML utilities
```

**Two deployment modes:**

| Mode | Entry point | How it runs |
|---|---|---|
| Polling | `goodreads_bot.py` | Long-polls Telegram via python-telegram-bot |
| Vercel Webhook | `api/webhook.py` | Vercel receives HTTPS POST from Telegram |

## 🤖 Bot Commands

| Command | Description |
|---|---|
| `/start` | Show welcome message |
| `/help` | Show help and usage guide |
| `/search <query>` | Search for books by title, author, or ISBN |

## 🔍 Features

- 🔎 **Multi-source search** – Google Books, Hardcover.app, iTunes & more
- 📖 **Rich book data** – titles, authors, descriptions, ratings, covers
- 🖼️ **High-resolution images** – iTunes-quality covers
- 💬 **Inline search** – use `@BotUsername <book name>` from any chat
- 📱 **Telegram-native** – works in private chats, groups & channels

## 💬 Inline Search

No commands needed — search from any Telegram chat:

1. Type `@BotUsername <book name>` in any chat's message box
2. Pick a result from the list
3. Tap ⏳ on a result to expand full details (ISBN, pages, year, genres & more)

## 🚀 Deployment

### Option 1 — Polling (local / VPS / Docker)

**Prerequisites:** Python 3.12+

```bash
# Clone the repository
git clone https://github.com/Devil-breaker/Multi-source-Books-Search-Bot.git
cd Multi-source-Books-Search-Bot

# Install dependencies
pip install -r requirements.txt

# Create .env file
cp env.example .env
```

Edit `.env` and add your credentials:

```env
TELEGRAM_BOT_TOKEN=123456789:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
GOOGLE_BOOKS_API_KEY=your_google_books_api_key_here
HARDCOVER_API_KEY=your_hardcover_api_key_here
AZURE_TRANSLATOR_KEY=your_azure_translator_key_here
AZURE_TRANSLATOR_REGION=your_azure_translator_region_here
```

**Run:**

```bash
py -3.12 goodreads_bot.py
```

**Run with Docker:**

```bash
docker build -t books-bot .
docker run --env-file .env books-bot
```

---

### Option 2 — Vercel Webhook

The bot runs as a serverless Vercel Python function. Telegram sends updates via HTTPS webhook — no long-running process needed.

#### Before deploying (one-time Telegram setup)

Set your bot's webhook to point at Vercel:

```
https://api.telegram.org/bot<YOUR_TOKEN>/setWebhook?url=https://your-project.vercel.app/api/webhook
```

Or use the helper script (from the `vercel` branch):

```bash
py -3.12 setup_webhook.py
```

#### Deploy via Vercel Dashboard

1. Go to [vercel.com](https://vercel.com) and sign in
2. Click **Add New → Project**
3. Import your GitHub repository (`Devil-breaker/Multi-source-Books-Search-Bot`)
4. In **Framework Preset**, select **Python** (or leave as Other)
5. Under **Build and Output Settings**, leave both fields at default (no build command needed for Python)
6. Click **Environment Variables** and add:
   - `TELEGRAM_BOT_TOKEN` — your Telegram bot token
   - `GOOGLE_BOOKS_API_KEY` — *(optional)*
   - `HARDCOVER_API_KEY` — *(optional)*
   - `AZURE_TRANSLATOR_KEY` — *(optional; only used for non-English descriptions)*
   - `AZURE_TRANSLATOR_REGION` — *(set when required by your Azure resource)*
   - `WEBHOOK_SECRET` — *(optional, but recommended)* a random secret string to verify incoming webhook requests
7. Click **Deploy**

After the first deploy, set your Telegram bot's webhook URL (see "Before deploying" above), substituting `your-project.vercel.app` with your actual Vercel deployment URL.

#### Deploy via Vercel CLI

```bash
# Install Vercel CLI
npm install -g vercel

# Login (opens browser)
vercel login

# Go to the vercel branch (has api/ webhook files)
git checkout vercel

# Pull the latest remote changes
git pull origin vercel

# Deploy to preview
vercel

# Deploy to production
vercel 
# On the vercel branch
git pull origin vercel
vercel --prod
```

#### Vercel environment variables (CLI)

```bash
# Add each variable
vercel env add TELEGRAM_BOT_TOKEN
vercel env add GOOGLE_BOOKS_API_KEY
vercel env add HARDCOVER_API_KEY
vercel env add WEBHOOK_SECRET    # optional but recommended

# After adding variables, redeploy to apply them
vercel --prod
```

#### Cron job (keep-warm heartbeat)

`api/cron.py` runs every 10 minutes (configured in `vercel.json`) to flush the Hardcover API cache and prevent cold starts. The Vercel Cron Job should be automatically enabled from `vercel.json`. If not, add it manually under **Storage → Cron Jobs** in your Vercel dashboard.

---

## 📁 Project Structure

```
.
├── goodreads_bot.py           # Polling entry point (imports src/handlers.py)
├── goodreads_bot_advanced.py  # Legacy all-in-one (kept for reference)
├── src/
│   ├── handlers.py            # GoodreadsBot class — single source of truth
│   ├── aggregator.py          # MultiSourceBookAggregator
│   ├── search.py              # Search helpers + Goodreads URL builder
│   └── utils.py               # Logger, HEADERS, HTML utilities
├── api/
│   ├── webhook.py             # Vercel webhook entry point
│   └── cron.py                # Vercel cron job (keep-warm + cache flush)
├── vercel.json                # Vercel config (builds, crons, CORS headers)
├── requirements.txt           # Python dependencies
├── Dockerfile                 # Docker image (Python 3.12)
├── env.example                # Environment variable template
└── .gitignore                 # Git ignore rules (.env, __pycache__, .claude/, etc.)
```

## 🛠️ Dependencies

- [python-telegram-bot](https://python-telegram-bot.org/) v21.1 — Telegram Bot API
- [requests](https://docs.python-requests.org/) — HTTP client
- [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/) — HTML parsing
- [python-dotenv](https://pypi.org/project/python-dotenv/) — .env support
- [lxml](https://lxml.de/) — XML/HTML parser
- [storygraph-api](https://pypi.org/project/storygraph-api/) — StoryGraph ratings *(optional)*