"""Both marketplaces behind one adapter.

Polls every market that is switched on (MARKETS, or /market in Discord) and not
skipped, and merges their listings. One market failing never stops the other: its
error is kept in `errors`, it backs off on its own (a BloxyMarket 429 does not slow
BHOPP down), and an error is only raised when every polled market failed.

Buys, balances and sessions go to the listing's own marketplace.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable

from ..http import RateLimited
from ..models import ClaimOutcome, ClaimStatus, Listing, MarketplaceStatus
from .base import MarketplaceAdapter

log = logging.getLogger(__name__)
KNOWN = ("bhopp", "bloxymarket")
MAX_BACKOFF = 300.0


@dataclass
class PollResult:
    listings: list[Listing]
    complete: dict[str, set[str]]   # market polled successfully -> its listing ids
    polled: set[str]                # markets asked this time
    failures: list[Exception]

    @property
    def all_failed(self) -> bool:
        return bool(self.polled) and not self.complete


class MultiMarketAdapter(MarketplaceAdapter):
    name = "markets"
    supports_only = True

    def __init__(self, adapters: dict[str, MarketplaceAdapter],
                 enabled: Callable[[], set[str]], base_interval: float = 1.0):
        self.adapters = adapters
        self.enabled = enabled
        self.base_interval = max(base_interval, 1.0)
        super().__init__()
        self.status = MarketplaceStatus("markets", self.capabilities)
        self.errors: dict[str, str] = {}
        self.failures: dict[str, int] = {}
        self.rate_limited: dict[str, float] = {}   # market -> retry-after seconds
        self._next_ok: dict[str, float] = {}
        # Markets whose last poll fully succeeded -> the listing ids they returned.
        self.last_complete: dict[str, set[str]] = {}
        self.last_polled: set[str] = set()

    def adapter(self, market: str) -> MarketplaceAdapter | None:
        return self.adapters.get(market)

    def can_claim(self, listing: Listing) -> bool:
        a = self.adapters.get(listing.marketplace)
        return a is not None and a.capabilities.claim

    def claim_reason(self, listing: Listing) -> str:
        a = self.adapters.get(listing.marketplace)
        return a.capabilities.claim_unavailable_reason if a else "unknown marketplace"

    def active(self) -> list[str]:
        return [m for m in KNOWN if m in self.enabled() and m in self.adapters]

    # ---- monitoring

    async def connect(self):
        for a in self.adapters.values():
            await a.connect()

    async def close(self):
        for a in self.adapters.values():
            await a.close()

    def _failed(self, name: str, exc: Exception):
        n = self.failures[name] = self.failures.get(name, 0) + 1
        if isinstance(exc, RateLimited):
            wait = max(exc.retry_after, 30.0)
            self.rate_limited[name] = wait
        else:
            wait = 0.0 if n < 2 else min(MAX_BACKOFF, self.base_interval * 2 ** (n - 1))
        self._next_ok[name] = time.monotonic() + wait
        msg = f"{type(exc).__name__}: {exc}"
        if self.errors.get(name) != msg:
            log.warning("%s poll failed: %s%s", name, exc,
                        f"; next try in {wait:.0f}s" if wait else "")
        self.errors[name] = msg

    def wait_time(self, name: str) -> float:
        """Seconds until `name` may be polled again after failures (0 = now)."""
        return max(0.0, self._next_ok.get(name, 0.0) - time.monotonic())

    async def poll(self, only: set[str] | None = None,
                   markets: set[str] | None = None) -> "PollResult":
        """Poll the given (default: all active) markets that are not backing off.
        Never raises for a market's failure (see PollResult). Safe to call concurrently:
        each market is polled by its own loop in the service."""
        now = time.monotonic()
        names = [n for n in self.active() if (markets is None or n in markets)
                 and self._next_ok.get(n, 0.0) <= now]
        if not names:
            return PollResult([], {}, set(), [])
        complete: dict[str, set[str]] = {}

        async def one(name):
            a = self.adapters[name]
            return await (a.get_listings(only=only) if only is not None
                          else a.get_listings())

        results = await asyncio.gather(*(one(n) for n in names), return_exceptions=True)
        out, failures = [], []
        for name, res in zip(names, results):
            if isinstance(res, BaseException):
                if not isinstance(res, Exception):
                    raise res
                failures.append(res)
                self._failed(name, res)
            else:
                self.errors.pop(name, None)
                self.failures.pop(name, None)
                self.rate_limited.pop(name, None)
                self._next_ok.pop(name, None)
                complete[name] = {l.listing_id for l in res}
                out.extend(res)
        self.last_complete, self.last_polled = complete, set(names)
        return PollResult(out, complete, set(names), failures)

    async def get_listings(self, only: set[str] | None = None) -> list[Listing]:
        res = await self.poll(only)
        if res.failures and not res.complete:
            raise res.failures[0]
        return res.listings

    async def lookup(self, item: str, market: str = "both") -> list[Listing]:
        names = [n for n in self.active() if market in ("both", n)]
        results = await asyncio.gather(*(self.adapters[n].lookup(item) for n in names),
                                       return_exceptions=True)
        out, failures = [], []
        for name, res in zip(names, results):
            if isinstance(res, Exception):
                failures.append(res)
            elif isinstance(res, BaseException):
                raise res
            else:
                out.extend(res)
        if failures and len(failures) == len(names):
            raise failures[0]
        return out

    async def get_listing_details(self, listing: Listing) -> Listing | None:
        a = self.adapters.get(listing.marketplace)
        return await a.get_listing_details(listing) if a else None

    def item_names(self) -> list[str]:
        names = set()
        for n in self.active():
            names.update(self.adapters[n].item_names())
        return sorted(names)

    async def refresh_names(self):
        for n in self.active():
            try:
                await self.adapters[n].refresh_names()
            except Exception as e:  # noqa: BLE001 - autocomplete only
                log.debug("%s name refresh failed: %s", n, e)

    # ---- buying: the listing's own marketplace

    async def claim_listing(self, listing: Listing, idem_key: str) -> ClaimOutcome:
        if not self.can_claim(listing):
            return ClaimOutcome(ClaimStatus.SKIPPED, detail=self.claim_reason(listing))
        return await self.adapters[listing.marketplace].claim_listing(listing, idem_key)

    async def reconcile_claim(self, listing: Listing):
        a = self.adapters.get(listing.marketplace)
        if a is None:
            return ClaimStatus.UNKNOWN, None
        return await a.reconcile_claim(listing)
