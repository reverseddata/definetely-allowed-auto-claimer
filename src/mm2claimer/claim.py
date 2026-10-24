"""Claim engine: buy a watched item the moment it fits, as often as it keeps fitting.

There is no pause after a buy. Buying stops only when the balance runs out or a
limit is hit (MAX_DAILY_SPEND, MAX_CLAIM_PRICE, MAX_CLAIM_QUANTITY, a watch's own
max price / quantity), all of which /limits in Discord can change.

Order of operations for one buy (never re-ordered):
  1. refuse if this marketplace has an unresolved (unclear) buy
  2. size it: units listed, the watch's and global caps, what the balance affords
  3. reserve: idempotency key + daily cap, atomically, in SQLite
  4. revalidate: fetch fresh state; still listed, same item, price within limits
  5. buy: one request, bounded by a timeout
  6. outcome:
       CLAIMED  -> record what was charged, ping Discord, keep going
       REJECTED -> record, alert with the site's own words, keep going
                   (a "no balance" refusal stops buying there until it is topped up)
       UNKNOWN  -> ask the site (reconcile); if still unclear, hold that marketplace
                   and alert, because a blind retry could buy twice
Buys run one at a time. Only a request that provably never left the machine
(RequestNotSent) is retried.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .config import Settings, WatchEntry
from .http import MarketplaceError, RequestNotSent
from .marketplaces.base import MarketplaceAdapter
from .models import ClaimOutcome, ClaimStatus, Listing
from .notify import DiscordNotifier, label, money
from .store import Hold, Store, utc_day
from .watchlist import Limits

log = logging.getLogger(__name__)
SAFE_RETRIES = 2
# A balance older than this is read again before sizing a buy.
BALANCE_MAX_AGE = 120.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _listing_from_row(row) -> Listing | None:
    """Rebuild enough of an unclear buy's listing to look for it on the site."""
    try:
        handoff = json.loads(row["handoff"] or "{}")
        return Listing(marketplace=row["marketplace"], listing_id=row["listing_id"],
                       slug=handoff.get("slug") or row["listing_id"], name=row["name"],
                       price=Decimal(row["unit_price"]), unit=row["unit"], available=True,
                       quantity=int(row["quantity"] or 1), extra=handoff.get("extra") or {})
    except (ValueError, TypeError, KeyError, ArithmeticError):
        return None


def _adapter_for(adapter: MarketplaceAdapter, market: str) -> MarketplaceAdapter | None:
    pick = getattr(adapter, "adapter", None)
    return pick(market) if pick else adapter


class ClaimEngine:
    def __init__(self, settings: Settings, store: Store, adapter: MarketplaceAdapter,
                 notifier: DiscordNotifier, limits: Limits | None = None):
        self.s = settings
        self.store = store
        self.adapter = adapter
        self.notifier = notifier
        self.limits = limits or Limits(settings, store)
        self._lock = asyncio.Lock()
        self._cap_alert_day: str | None = None
        # market -> (balance, monotonic time read)
        self.balances: dict[str, tuple[Decimal, float]] = {}

    # ------------------------------------------------------------ balance

    async def refresh_balance(self, market: str) -> Decimal | None:
        a = _adapter_for(self.adapter, market)
        if a is None or not a.capabilities.claim:
            return None
        try:
            value = await asyncio.wait_for(a.get_balance(), self.s.request_timeout_seconds + 5)
        except Exception as e:  # noqa: BLE001 - a buy without a fresh balance still works
            log.debug("%s balance read failed: %s", market, e)
            return None
        self.balances[market] = (value, time.monotonic())
        return value

    async def _balance(self, market: str) -> Decimal | None:
        cached = self.balances.get(market)
        if cached and time.monotonic() - cached[1] <= BALANCE_MAX_AGE:
            return cached[0]
        return await self.refresh_balance(market)

    def _spend(self, market: str, amount: Decimal):
        cached = self.balances.get(market)
        if cached:
            self.balances[market] = (max(Decimal(0), cached[0] - amount), cached[1])

    def _no_balance(self, listing: Listing, balance: Decimal | None, why: str):
        """Stop buying on this market until the balance is read again (the background
        refresh, every BALANCE_REFRESH_SECONDS), and say so once per cooldown."""
        if balance is not None:
            self.balances[listing.marketplace] = (balance, time.monotonic())
        site = label(listing.marketplace)
        self.notifier.system(f"💸 Out of balance on {site}",
                             f"{why} Not buying {listing.name} "
                             f"({money(listing.price, listing.unit)}). Buying on {site} "
                             "resumes by itself once the balance covers it.", "warn",
                             key=f"nobalance:{listing.marketplace}")

    # ------------------------------------------------------------ claim

    def eligible(self, listing: Listing, watch: WatchEntry) -> tuple[bool, str]:
        if not listing.available:
            return False, "not listed"
        if listing.quantity < watch.min_qty:
            return False, f"only {listing.quantity} at this price, minimum is {watch.min_qty}"
        if not watch.price_ok(listing.price):
            return False, f"price {listing.price} above the watch's max {watch.max_price}"
        cap = self.limits.max_claim_price
        if cap is not None and listing.price > cap:
            return False, f"price {listing.price} above MAX_CLAIM_PRICE {cap}"
        return True, ""

    async def attempt(self, listing: Listing, watch: WatchEntry) -> ClaimOutcome:
        async with self._lock:
            market = listing.marketplace
            if self.store.get_hold(market) is not None:
                return ClaimOutcome(ClaimStatus.SKIPPED,
                                    detail=f"an unclear buy on {label(market)} is being checked")
            can_claim = getattr(self.adapter, "can_claim", None)
            if not (can_claim(listing) if can_claim else self.adapter.capabilities.claim):
                reason = getattr(self.adapter, "claim_reason", None)
                return ClaimOutcome(ClaimStatus.SKIPPED,
                                    detail=reason(listing) if reason else
                                    self.adapter.capabilities.claim_unavailable_reason)
            ok, why = self.eligible(listing, watch)
            if not ok:
                return ClaimOutcome(ClaimStatus.SKIPPED, detail=why)

            want = await self._want(listing, watch)
            if isinstance(want, ClaimOutcome):
                return want
            # One key per detection: re-submitting the same sighting can never buy twice.
            key = (f"{market}:{listing.listing_id}:{listing.price}:"
                   f"{listing.detected_at.timestamp():.6f}")
            res = self.store.reserve_claim(idem_key=key, listing=listing,
                                           daily_limit=self.limits.max_daily_spend,
                                           quantity=want)
            if not res.ok:
                if res.reason.startswith("daily cap") and self._cap_alert_day != utc_day():
                    self._cap_alert_day = utc_day()
                    self.notifier.system("Daily spend cap reached",
                                         f"Not buying {listing.name}: {res.reason}. "
                                         "Buying resumes tomorrow (UTC), or raise it with "
                                         "/limits.", "warn")
                log.info("buy skipped for %s: %s", listing.name, res.reason)
                return ClaimOutcome(ClaimStatus.SKIPPED, detail=res.reason)

            fresh = await self._revalidate(listing, watch)
            if isinstance(fresh, str):
                self.store.update_claim(key, ClaimStatus.SKIPPED, detail=fresh)
                log.info("buy skipped for %s: %s", listing.name, fresh)
                return ClaimOutcome(ClaimStatus.SKIPPED, detail=fresh)
            if fresh.price > listing.price:
                # Dearer than when sized and reserved: the reservation no longer covers it.
                why = f"price rose to {fresh.price} before the buy"
                self.store.update_claim(key, ClaimStatus.SKIPPED, detail=why)
                return ClaimOutcome(ClaimStatus.SKIPPED, detail=why)
            qty = max(1, min(res.quantity, fresh.quantity))
            if qty < watch.min_qty:
                why = f"only {qty} left to buy, minimum is {watch.min_qty}"
                self.store.update_claim(key, ClaimStatus.SKIPPED, detail=why)
                log.info("buy skipped for %s: %s", listing.name, why)
                return ClaimOutcome(ClaimStatus.SKIPPED, detail=why)
            fresh = replace(fresh, quantity=qty, detected_at=listing.detected_at,
                            extra={**listing.extra, **fresh.extra})
            if qty != res.quantity or fresh.price != listing.price:
                self.store.update_claim(key, ClaimStatus.PENDING, quantity=qty,
                                        total=fresh.price * qty)

            outcome = await self._send(fresh, key)
            if outcome.status is ClaimStatus.UNKNOWN:
                outcome = await self._reconcile(fresh, outcome)
            self._finish(fresh, key, outcome, watch)
            return outcome

    async def _want(self, listing: Listing, watch: WatchEntry) -> int | ClaimOutcome:
        """Units to ask for: everything at this price, within the watch's and global
        caps and what the balance affords."""
        want = max(1, listing.quantity)
        if watch.max_qty:
            want = min(want, watch.max_qty)
        if self.limits.max_claim_quantity:
            want = min(want, self.limits.max_claim_quantity)
        balance = await self._balance(listing.marketplace)
        if balance is not None and listing.price > 0:
            afford = int(balance // listing.price)
            if afford < max(1, watch.min_qty):
                why = (f"balance {money(balance, listing.unit)} does not cover "
                       f"{max(1, watch.min_qty)} at {money(listing.price, listing.unit)}")
                if afford < 1:
                    self._no_balance(listing, balance, f"Balance is "
                                                       f"{money(balance, listing.unit)}.")
                log.info("buy skipped for %s: %s", listing.name, why)
                return ClaimOutcome(ClaimStatus.SKIPPED, detail=why)
            want = min(want, afford)
        return want

    async def _revalidate(self, listing: Listing, watch: WatchEntry) -> Listing | str:
        a = _adapter_for(self.adapter, listing.marketplace)
        if getattr(a, "server_checks_price", False):
            # The buy itself re-checks (BloxyMarket: expected_price per unit).
            ok, why = self.eligible(listing, watch)
            return listing if ok else f"revalidation: {why}"
        try:
            fresh = await self.adapter.get_listing_details(listing)
        except MarketplaceError as e:
            return f"revalidation failed: {e}"
        if fresh is None:
            return "sold out before the buy"
        if fresh.listing_id != listing.listing_id or fresh.marketplace != listing.marketplace:
            return "listing identity changed"
        ok, why = self.eligible(fresh, watch)
        return fresh if ok else f"revalidation: {why}"

    async def _send(self, listing: Listing, key: str) -> ClaimOutcome:
        for attempt in range(SAFE_RETRIES + 1):
            try:
                return await asyncio.wait_for(self.adapter.claim_listing(listing, key),
                                              self.s.request_timeout_seconds + 5)
            except RequestNotSent as e:
                if attempt == SAFE_RETRIES:
                    return ClaimOutcome(ClaimStatus.REJECTED, detail=f"could not connect: {e}")
                await asyncio.sleep(1 + attempt)
            except asyncio.TimeoutError:
                return ClaimOutcome(ClaimStatus.UNKNOWN, detail="buy request timed out")
            except Exception as e:  # noqa: BLE001 - outcome unknown, must not retry
                log.exception("buy request raised")
                return ClaimOutcome(ClaimStatus.UNKNOWN, detail=f"{type(e).__name__}: {e}")
        return ClaimOutcome(ClaimStatus.UNKNOWN, detail="unreachable")

    async def _reconcile(self, listing: Listing, outcome: ClaimOutcome) -> ClaimOutcome:
        try:
            state, found_id = await self.adapter.reconcile_claim(listing)
        except Exception as e:  # noqa: BLE001
            log.warning("reconcile failed: %s", e)
            return outcome
        if state is ClaimStatus.CLAIMED:
            a = _adapter_for(self.adapter, listing.marketplace)
            return ClaimOutcome(ClaimStatus.CLAIMED, found_id or outcome.claim_id,
                                "confirmed on the site after an unclear answer",
                                username=a.account if a else "",
                                quantity=max(1, listing.quantity))
        return outcome

    def _finish(self, listing: Listing, key: str, outcome: ClaimOutcome, watch: WatchEntry):
        market = listing.marketplace
        a = _adapter_for(self.adapter, market)
        if not outcome.username and a is not None and a.account:
            outcome = replace(outcome, username=a.account)
        handoff = {"link": outcome.link, "username": outcome.username,
                   "seller": outcome.seller, "slug": listing.slug, "extra": listing.extra}
        if outcome.status is ClaimStatus.CLAIMED:
            qty = outcome.quantity or max(1, listing.quantity)
            total = outcome.charged if outcome.charged is not None else listing.price * qty
            self.store.update_claim(key, ClaimStatus.CLAIMED, claim_id=outcome.claim_id,
                                    detail=outcome.detail, handoff=handoff, quantity=qty,
                                    total=total)
            self._spend(market, total)
            log.info("BOUGHT %dx %s on %s at %s", qty, listing.name, market, listing.price)
            self.notifier.claim_succeeded(listing, outcome)
            self._check_charged(listing, watch, outcome)
        elif outcome.status is ClaimStatus.UNKNOWN:
            self.store.update_claim(key, ClaimStatus.UNKNOWN, claim_id=outcome.claim_id,
                                    detail=outcome.detail, handoff=handoff)
            mins = self.s.claim_hold_timeout_minutes
            self.store.set_hold(Hold(market, key, listing.name, _now(),
                                     _now() + timedelta(minutes=mins) if mins > 0 else None))
            log.warning("buy outcome UNKNOWN for %s: %s", listing.name, outcome.detail)
            self.notifier.claim_failed(listing, outcome)
        else:
            self.store.update_claim(key, outcome.status, claim_id=outcome.claim_id,
                                    detail=outcome.detail)
            log.info("buy %s for %s: %s", outcome.status.value, listing.name, outcome.detail)
            if outcome.no_balance:
                self._no_balance(listing, Decimal(0), f"{label(market)} said: "
                                                      f"{outcome.detail}.")
            else:
                self.notifier.claim_failed(listing, outcome)

    def _check_charged(self, listing: Listing, watch: WatchEntry, outcome: ClaimOutcome):
        """The buy request cannot always lock the price. If what the site charged is
        above the limits, say so loudly: the order can still be cancelled."""
        charged = outcome.charged
        if charged is None:
            return
        qty = outcome.quantity or max(1, listing.quantity)
        each = charged / qty
        problems = []
        if charged > listing.price * qty:
            problems.append(f"charged {money(charged, listing.unit)} for {qty}, listed at "
                            f"{money(listing.price, listing.unit)} each")
        if watch.max_price is not None and each > watch.max_price:
            problems.append(f"above the watch's max {watch.max_price}")
        cap = self.limits.max_claim_price
        if cap is not None and each > cap:
            problems.append(f"above MAX_CLAIM_PRICE {cap}")
        if problems:
            site = label(listing.marketplace)
            log.warning("bought %s outside limits: %s", listing.name, "; ".join(problems))
            self.notifier.system(f"⚠️ {listing.name}: price moved",
                                 "The order was placed but " + "; ".join(problems)
                                 + f". Cancel it on {site} if you don't want it.", "error")

    # ------------------------------------------------------------ unclear buys

    async def check_holds(self, markets: set[str] | None = None) -> set[str]:
        """Resolve unclear buys (on `markets`, default all). Returns the markets still
        held afterwards."""
        held = set()
        for hold in self.store.holds():
            if markets is not None and hold.market not in markets:
                continue
            if not await self._check_hold(hold):
                continue
            held.add(hold.market)
        return held

    async def _check_hold(self, hold: Hold) -> bool:
        row = self.store.claim(hold.idem_key)
        listing = _listing_from_row(row) if row is not None else None
        state, found_id = ClaimStatus.UNKNOWN, None
        if listing is not None:
            try:
                state, found_id = await self.adapter.reconcile_claim(listing)
            except Exception as e:  # noqa: BLE001
                log.debug("reconcile of %s failed: %s", hold.name, e)
        site = label(hold.market)
        if state is ClaimStatus.CLAIMED:
            self.store.update_claim(hold.idem_key, ClaimStatus.CLAIMED, claim_id=found_id,
                                    detail="found on the site after an unclear answer")
            a = _adapter_for(self.adapter, hold.market)
            self.notifier.claim_succeeded(listing, ClaimOutcome(
                ClaimStatus.CLAIMED, found_id, "found after an unclear answer",
                username=a.account if a else "", quantity=listing.quantity))
            self.resume(hold.market, f"the unclear buy of {hold.name} went through")
            return False
        if hold.until is not None and _now() >= hold.until:
            if row is not None:
                # No final answer: it may still have gone through, so it keeps counting.
                self.store.update_claim(hold.idem_key, ClaimStatus.TIMED_OUT,
                                        detail="never showed up on the site")
            self.resume(hold.market, f"the buy of {hold.name} never showed up on {site} "
                        f"within {self.s.claim_hold_timeout_minutes:g} min; check {site}",
                        level="warn")
            return False
        return True

    def resume(self, market: str, reason: str, level: str = "ok"):
        self.store.clear_hold(market)
        log.info("buying on %s resumed: %s", market, reason)
        self.notifier.system(f"▶️ Buying on {label(market)} resumed", reason, level)

    def reconcile_on_startup(self):
        """A PENDING row at startup means we crashed mid-buy: outcome unknown."""
        for row in self.store.open_claims():
            if row["status"] == ClaimStatus.PENDING.value:
                self.store.update_claim(row["idem_key"], ClaimStatus.UNKNOWN,
                                        detail="process stopped mid-buy")
            market = row["marketplace"]
            if self.store.get_hold(market) is None:
                mins = self.s.claim_hold_timeout_minutes
                self.store.set_hold(Hold(market, row["idem_key"], row["name"], _now(),
                                         _now() + timedelta(minutes=mins) if mins else None))
                self.notifier.system(
                    "⚠️ Unfinished buy found at startup",
                    f"A buy of {row['name']} on {label(market)} had no clear answer when the "
                    f"process stopped. Buying on {label(market)} stays paused until the bot "
                    "sees whether it went through, it times out, or you run "
                    "`mm2claimer resume`.", "warn")
