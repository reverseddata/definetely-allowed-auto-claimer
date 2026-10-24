# Railway template: what to enter

For the maintainer: create the template once at https://railway.com/compose (or
Workspace → Templates → New Template), then add its Deploy button to the README.

## Service

| Field | Value |
|---|---|
| Source | Docker image `ghcr.io/reverseddata/definetely-allowed-auto-claimer:latest` |
| Name | `mm2-claimer` |
| Start command | leave empty (the image runs `mm2claimer run`) |
| Networking | none (no public domain, no port) |
| Replicas | 1 |
| Restart policy | On failure |

## Volume

| Field | Value |
|---|---|
| Mount path | `/app/data` |

## Variables

| Name | Default | Required | Description shown to users |
|---|---|---|---|
| `MODE` | `monitor` | yes | `monitor` = only ping Discord. `claim` = buy on your accounts. Start with monitor. |
| `MARKETS` | `both` | no | bhopp, bloxymarket or both. Switch later with /market. |
| `DISCORD_WEBHOOK_URL` | | yes | Discord channel webhook for alerts (Channel settings → Integrations → Webhooks). |
| `DISCORD_MENTION_USER_ID` | | yes | Your Discord user ID. Pinged on buys, and the only user allowed to use the commands. |
| `DISCORD_BOT_TOKEN` | | yes | Bot token from discord.com/developers, for the slash commands. |
| `DISCORD_GUILD_ID` | | no | Your server ID, so the commands appear instantly. |
| `BHOPP_TOKEN` | | no (to buy on BHOPP) | localStorage `bhopp.token` on bhopp.com (README "Getting your sessions"). Secret. |
| `BLOXYMARKET_COOKIE` | | no (to buy on BloxyMarket) | Cookie header of a logged-in bloxymarket.com tab. Secret. |
| `MAX_DAILY_SPEND` | | no (recommended) | Dollars per UTC day, both sites together. /limits can change it. |
| `MAX_CLAIM_PRICE` | | no | Most per unit on any buy, in dollars. |
| `POLL_INTERVAL_SECONDS` | `10` | no | Pause between polls of each site. 0 = back to back. |
| `RAILWAY_RUN_UID` | `0` | yes (keep) | Lets the app write to the Railway volume. Don't change. |

Do not put any of your own values in the template. Every user enters their own.

## Notes

- The image package must be **public**. On GitHub: your profile → Packages →
  `definetely-allowed-auto-claimer` → Package settings → Change visibility → Public.
  GHCR packages start out private, and Railway can't pull a private image without
  credentials.
- A new image is published on every push to `main` (`latest`) and for `v*` tags.
  For predictable updates, point the template at a version tag such as `v0.1.0`.
