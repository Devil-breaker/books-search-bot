# Local Mini App preview

The Mini App is served by the existing Flask keep-alive server at
`/miniapp/`; its API is under `/miniapp/api/`. No separate frontend server or
build step is needed.

Live suggestions use the same fast provider mix as inline search: Hardcover is
the primary catalog and iTunes supplements covers/results. Google Books is
queried as a fallback when Hardcover returns no matches, and again for fuller
metadata only after a user opens a result. This avoids calling Google Books for
every intermediate query while retaining a fallback for books missing from
Hardcover.

## Recommendations

The standalone recommendations module is served by
`POST /miniapp/api/recommendations`. It accepts any combination of `read`,
`liked`, `genres`, and `moods`; at least one non-empty value is required. The
endpoint uses Hardcover as its primary candidate source and Google Books for a
small amount of catalog diversification. It ranks by preference fit and a
Bayesian rating estimate, keeps popularity as a small tie-break, and reserves
room for a relevant, well-rated lower-discovery title when one is available.
Results are cached in process for 30 minutes; user preferences are not stored
in a database. Recommendation requests have a separate, tighter per-user rate
limit because each request can query multiple providers.

## Bookshelf storage

Set `MONGODB_URI` (and optionally `MONGODB_DB_NAME`, default `annie_db`) to sync My Books and Favourites across devices. The first bookshelf write creates the `bookshelf_users` collection automatically. Each Telegram account is limited to 100 unique books shared between both lists. One atomic document update handles an add or move, and one update handles a single or bulk removal. Opening the bookshelf reads once and the Mini App reuses that response for 30 seconds; local sorting, searching, and tab changes do not make database or catalog requests. Descriptions are stored in bounded snapshots to avoid refetching full details each time a saved book is opened. With no MongoDB URI, the app keeps using its per-user browser cache.

## Open the screen locally

Start the bot with the project's virtual environment and open
`http://127.0.0.1:8080/miniapp/` in a browser (or use the value of `PORT`). This
lets you preview the layout. Searches require a Telegram Web App session, so a
plain browser preview will display an authentication hint instead of calling
the API.

## Test search inside Telegram

Telegram requires a publicly reachable HTTPS URL for a Mini App. For a local
end-to-end test:

1. Use a separate test bot/token so the running production bot is unaffected.
2. Run this project locally and expose its port through an HTTPS tunnel.
3. In BotFather, configure that test bot's Main Mini App URL as
   `https://<your-tunnel-host>/miniapp/`.
4. Open the test bot's Mini App. It sends Telegram's signed `initData` to the
   local API, which verifies it using that test bot's `TELEGRAM_BOT_TOKEN`.

Run only one polling process per bot token while testing. The web page and API
use the same origin, so no cross-origin configuration is needed.
