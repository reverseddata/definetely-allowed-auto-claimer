"""Where the watchlist and the spending limits come from.

Without a Discord bot, the watchlist is the WATCH variable / config.toml, fixed at
startup. With DISCORD_BOT_TOKEN set, the list lives in the database (on the
DATA_DIR volume) and Discord commands change it. On the first start with an empty
database the WATCH / config.toml entries are copied in once; after that the
database is the list, so editing WATCH no longer changes anything (use /watch add
and remove).

Limits work the same way: MAX_* in .env are the defaults, /limits in Discord
overrides them (stored in the database), "default" goes back to .env.
"""
from __future__ import annotations

from decimal import Decimal

from .config import Settings, WatchEntry
from .store import Store

SEEDED_KEY = "watch_seeded"
PAUSED_KEY = "paused"
MARKETS_KEY = "markets"
LIMIT_PREFIX = "limit:"
OFF = "off"


class Limits:
    """The limits in force right now: a Discord override, else the .env value."""

    def __init__(self, settings: Settings, store: Store):
        self.s = settings
        self.store = store

    def _override(self, name: str) -> str | None:
        return self.store.get_kv(LIMIT_PREFIX + name)

    def _dec(self, name: str, default: Decimal | None) -> Decimal | None:
        raw = self._override(name)
        if raw is None:
            return default
        return None if raw == OFF else Decimal(raw)

    @property
    def max_claim_price(self) -> Decimal | None:
        return self._dec("max_claim_price", self.s.max_claim_price)

    @property
    def max_daily_spend(self) -> Decimal | None:
        return self._dec("max_daily_spend", self.s.max_daily_spend)

    @property
    def max_claim_quantity(self) -> int:
        raw = self._override("max_claim_quantity")
        if raw is None:
            return self.s.max_claim_quantity
        return 0 if raw == OFF else int(raw)

    def set(self, name: str, value: str | None):
        """value: a number, OFF (no limit), or None (back to the .env default)."""
        if value is None:
            self.store.delete_kv(LIMIT_PREFIX + name)
        else:
            self.store.set_kv(LIMIT_PREFIX + name, value)

    def overridden(self, name: str) -> bool:
        return self._override(name) is not None


class WatchList:
    def __init__(self, settings: Settings, store: Store):
        self.s = settings
        self.store = store
        self.managed = bool(settings.discord_bot_token)
        if self.managed and self.store.get_kv(SEEDED_KEY) is None:
            for w in settings.watch:
                self.store.put_watch(w)
            self.store.set_kv(SEEDED_KEY, "1")

    def entries(self) -> list[WatchEntry]:
        """Read once per sweep: a change from Discord applies on the next poll."""
        return self.store.watch_entries() if self.managed else list(self.s.watch)

    @staticmethod
    def find(entries: list[WatchEntry], name: str, slug: str,
             market: str) -> WatchEntry | None:
        """A one-site entry beats a both-sites entry for the same item."""
        hits = [w for w in entries if w.matches(name, slug, market)]
        return next((w for w in hits if w.market == market), hits[0] if hits else None)

    # ---- which marketplaces are polled (MARKETS, overridden by /market)

    def markets(self) -> set[str]:
        saved = self.store.get_kv(MARKETS_KEY) if self.managed else None
        if saved:
            from .config import parse_markets
            parsed = parse_markets(saved)
            if parsed:
                return parsed
        return set(self.s.markets)

    def set_markets(self, markets: set[str]):
        self.store.set_kv(MARKETS_KEY, ",".join(sorted(markets)))

    # ---- pause from Discord

    @property
    def paused(self) -> bool:
        return self.store.get_kv(PAUSED_KEY) == "1"

    def set_paused(self, value: bool):
        if value:
            self.store.set_kv(PAUSED_KEY, "1")
        else:
            self.store.delete_kv(PAUSED_KEY)
