# definetely-allowed-auto-claimer (mm2claimer)

Watches [BHOPP](https://bhopp.com) and [BloxyMarket](https://bloxymarket.com) for
Murder Mystery 2 items. When a watched item becomes buyable within your limits, it
either pings Discord (`monitor` mode) or buys it on your account and pings Discord
(`claim` mode). You run it from Discord with slash commands.

What counts as "buyable": a watched item that

* is listed again after selling out,
* gets cheaper (a new cheapest offer), or gets more units at the cheapest price,
* or shows up for the first time while you watch it (a new release; you can watch a
  name before either site lists it).

There is **no pause after a buy**: it keeps buying while the item keeps fitting. It
stops only when the site balance runs out or a limit is hit (daily spend, max price,
max units), and you can set those limits from Discord.

* **BHOPP**: buys the cheapest offer that is not your own. The order is paid at
  checkout; the seller then trades the item to your Roblox account, and you confirm
  receipt on BHOPP. The alert links the order chat.
* **BloxyMarket**: buys the cheapest units, each with its expected price, so the site
  refuses rather than charge more. The items land in your BloxyMarket inventory;
  withdraw them there when you want them in Roblox.

> Use it on your own accounts only. Automating a site may break its terms of service.
> Purchases are final and you are responsible for what the bot buys. Start in
> `monitor` mode, and set `MAX_DAILY_SPEND`.

## Quick start (local)

```bash
python -m venv .venv
.venv/bin/pip install .            # Windows: .venv\Scripts\pip install .
cp .env.example .env               # then fill it in
mm2claimer check-config
mm2claimer probe                   # one live read: shows your watched items on both sites
mm2claimer balance                 # read-only: checks your sessions
mm2claimer run
```

## Choosing items

Prices are US dollars per unit on both sites (BloxyMarket coins are $1).

### From Discord (recommended, and the only way on Railway without redeploys)

| Command | Does |
|---|---|
| `/watch add item market max min_qty max_qty` | Watch an item, or change its limits (name autocompletes) |
| `/watch remove item market` | Stop watching it (on one site, or everywhere) |
| `/watch list` | Show what is watched |
| `/watch pause` / `/watch resume` | Stop / restart sniping |
| `/limits daily_spend max_price max_quantity` | Show or set the spending limits |
| `/balance` | Your account and balance on each site |
| `/market` | Show or switch markets: bhopp, bloxymarket, both |
| `/status` | Mode, markets, spend today, last poll, anything being checked |

Examples:

* `/watch add item:Harvester max:5` buys Harvester on both sites at $5 or less.
* `/watch add item:Harvester market:bloxymarket max:4.5` gives BloxyMarket its own price;
  a one-site entry beats the both-sites one.
* `/watch add item:Chroma Seer max:1 min_qty:3 max_qty:10` buys only when at least 3
  fit, and at most 10 per buy.
* `/limits daily_spend:25 max_price:50` caps spending at $25 per UTC day (both sites
  together) and any unit at $50. `off` removes a limit, `default` goes back to `.env`.

Quantities: a buy takes every unit at the cheapest price that your balance allows,
within `max_qty` and `MAX_CLAIM_QUANTITY`, and within what is left of the daily cap.
Dearer units are bought on the next polls if they still fit (the cheapest price
moves up to them).

Only the user in `DISCORD_MENTION_USER_ID` can run the commands; everyone else is
refused, and replies are visible only to you. Every command answers: it shows
"thinking…" at once, then the result, or after at most 90 s a note that the site was
too slow (nothing changed), or the error. It never ends in "The application did not
respond". Claim mode refuses an entry with no
price limit (its own `max`, or a global max price).

Setup (one time):

1. https://discord.com/developers/applications → **New Application** → **Bot** →
   **Reset Token**, copy it into `DISCORD_BOT_TOKEN`. No privileged intents are needed.
2. **OAuth2 → URL Generator**: scopes `bot` and `applications.commands`, no permissions.
   Open the URL and add the bot to your server.
3. Set `DISCORD_MENTION_USER_ID` to your own user ID, and `DISCORD_WEBHOOK_URL` to a
   channel webhook (alerts go there). Optionally set `DISCORD_GUILD_ID` (your server's
   ID) so the commands appear instantly instead of after a few minutes.

### Without the bot

Set `WATCH` in `.env`, comma-separated:

```
WATCH=Harvester<=5, Chroma Seer@bloxymarket<=1 x3-10, Embersickle@bhopp<=1.5
```

`@bhopp` / `@bloxymarket` limits an entry to one site, `<=N` is the max price,
`xMIN-MAX` the units per buy. Or use `[[watch]]` tables in `config.toml`
(see `config.example.toml`). With the bot, these are copied into its list on the
first start only; after that the Discord list counts.

## Getting your sessions

Claim mode needs a logged-in session for each site you buy on. A site without one
is alert-only. Put them in `.env` (or Railway variables) yourself; never paste them
in chat or commit them. Anyone with these values can spend your balance.

**BHOPP** (`BHOPP_TOKEN`): log in at bhopp.com, press F12 → **Application** (Chrome) /
**Storage** (Firefox) → **Local Storage** → `https://bhopp.com`, and copy the value of
`bhopp.token` (it starts with `eyJ`). Or in **Network**, click any request to
`api.sublimelistings.com` and copy what follows `authorization: Bearer `. The token
lasts 7 days and BHOPP never renews it: the bot warns in Discord a day before it
expires, and when BHOPP rejects it. Then paste a fresh one.

**BloxyMarket** (`BLOXYMARKET_COOKIE`): log in at bloxymarket.com, press F12 →
**Network**, refresh, click a request to `api.bloxymarket.com` such as `authenticate`
or `balance`, and under **Request Headers** copy the whole value after `cookie:`.
Put it in `.env` in double quotes: `BLOXYMARKET_COOKIE="..."`. Cookies the site
rotates are kept in `DATA_DIR`. Paste a new value when the bot reports the session
was rejected. Logging out on the site ends it.

Check both with `mm2claimer balance` (or `/balance`), which only reads.

## Polling and rate limits

`POLL_INTERVAL_SECONDS` (default 10, may be 0) is the pause between polls of each
site; each site has its own loop, so a slow one never holds the other back. While
you watch at most `SEARCH_QUERY_LIMIT` (6) names, a poll searches the site for each
name and parses only those items; with more names it reads the whole market.

* BHOPP answers fast; requests are spaced 0.2 s apart.
* BloxyMarket rate-limits by IP: requests are spaced `BLOXYMARKET_REQUEST_GAP_SECONDS`
  (default 3) apart, so with 3 watched names each is checked every ~9 s. Going lower
  risks a block (1 s got an IP blocked before). A purchase skips the gap: it never
  waits behind a poll. On a 429 only BloxyMarket backs off.

## Commands

| Command | What it does |
|---|---|
| `mm2claimer run` | Run the watcher |
| `mm2claimer check-config` | Validate settings (prints which secrets are set, never values) |
| `mm2claimer probe` | One live read of your watched items. Never buys |
| `mm2claimer balance` | Read-only: account names and balances |
| `mm2claimer status` | Last status snapshot |
| `mm2claimer claims` | Recent buys |
| `mm2claimer resume [market]` | Release a market held by an unclear buy, after checking the site |

## Deploying on Railway

### One click

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/2trSRg?referralCode=YMdSJg&utm_medium=integration&utm_source=template&utm_campaign=generic)

1. Click the button and sign in to Railway.
2. Fill in the three required variables: `DISCORD_WEBHOOK_URL`, `DISCORD_BOT_TOKEN`,
   `DISCORD_MENTION_USER_ID` (see "From Discord" above). That is enough for
   `monitor` mode. To buy, set `MODE=claim`, `BHOPP_TOKEN` and/or
   `BLOXYMARKET_COOKIE` (see "Getting your sessions"), and a `MAX_DAILY_SPEND`.
3. Deploy, then pick items with `/watch add` in Discord.

It runs the published image `ghcr.io/reverseddata/definetely-allowed-auto-claimer:latest`
with a volume on `/app/data` already attached. To update, redeploy the service.

### By hand

The image is built and published by GitHub Actions on every push to `main`.

1. In Railway: **New Project → Empty Project → New → Docker Image**, and enter
   `ghcr.io/reverseddata/definetely-allowed-auto-claimer:latest`. (Or **Deploy from
   GitHub repo**; the `Dockerfile` is used automatically.) No port or domain is
   needed; it is a worker.
2. **Variables**: `MODE`, `DISCORD_WEBHOOK_URL`, `DISCORD_MENTION_USER_ID`,
   `DISCORD_BOT_TOKEN`, and `BHOPP_TOKEN` and/or `BLOXYMARKET_COOKIE` for claim mode.
   Recommended: `MAX_DAILY_SPEND`, `MAX_CLAIM_PRICE`. Optional: `DISCORD_GUILD_ID`,
   `POLL_INTERVAL_SECONDS`. Paste values without quotes in Railway.
3. **Volume**: add one mounted at `/app/data`. It holds the ledger (what was bought
   today, for the daily cap), unclear buys being checked, the watchlist, the Discord
   limits, and the rotated BloxyMarket cookies. Set `RAILWAY_RUN_UID=0` (Railway
   volumes are owned by root; the image runs as a non-root user).
4. Watch the deploy logs for `BHOPP session OK` / `BloxyMarket session OK`, then add
   items with `/watch add`.
5. Keep **1 replica** and run only one copy per account (not Railway and your PC at
   once): two copies keep separate ledgers and would both buy.

## How it stays safe

* Every buy is reserved in SQLite (idempotency key, daily cap) before it is sent.
  One sighting can never buy twice, and buys run one at a time.
* Right before a buy the item is checked again: BHOPP re-reads the product's offers;
  BloxyMarket's purchase carries each unit's expected price, so the site itself
  refuses a changed price or a sold unit.
* An unclear answer (timeout, 5xx) is never retried. The bot looks for the purchase
  on the site (BHOPP open orders, BloxyMarket inventory); until it finds it or
  `CLAIM_HOLD_TIMEOUT_MINUTES` pass, buying on that site pauses. The other site
  keeps going. This survives restarts.
* A refusal is reported with the site's own words. "Not enough balance" stops
  buying on that site until the balance covers the item again.
* What the site actually charged is recorded; anything above your limits is flagged
  in Discord so you can cancel the order.
* Tokens and cookies are redacted from logs and Discord messages, and never sent
  anywhere but their own site.

## Which endpoints are verified

Read live (2026-10-25, public GETs): BHOPP `GET /market` (search, paging, offer
shape, prices in cents), `GET /products/{slug}/offers`, `GET /connect` (logged out);
BloxyMarket `GET /v1/marketplace` (paging, `display_name` search, unit shape).

Read from the sites' own JavaScript, not yet run against a logged-in account (the
first real buy is the test, so start with a small `MAX_DAILY_SPEND`): BHOPP
`GET /connect` (logged in, wallet), `POST /orders/checkout`, `GET /orders`;
BloxyMarket `GET /authenticate`, `GET /v1/balance`, `POST /v1/purchase`,
`GET /v2/inventory`.

## License

MIT
