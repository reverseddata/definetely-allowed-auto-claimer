"""The long-running watcher.

One loop per marketplace (so the slower one never holds the faster one back):
  resolve an unclear buy there (a market with one is not polled until it is resolved)
  poll it for the watched items, find the ones that just became buyable (listed
  again, cheaper, more units, new release), buy or notify

Failures are contained: a bad poll backs that market off, a Discord outage only
loses messages, a database error falls back to in-memory dedupe and refuses buys.
Balances are read in the background, so sizing a buy rarely waits on them.
The adapter is a MultiMarketAdapter (marketplaces/multi.py).
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import signal
import time
from datetime import datetime, timedelta, timezone

from . import __version__
from .claim import ClaimEngine
from .config import Settings
from .http import AuthError, HostLimit, HttpClient, RateLimited
from .lock import InstanceLock
from .marketplaces.base import MarketplaceAdapter
from .models import ClaimStatus, Listing
from .notify import DiscordNotifier, label
from .store import Change, Store, StoreError, utc_day
from .watchlist import Limits, WatchList

log = logging.getLogger(__name__)
DOWN_AFTER = 5            # consecutive failures before a "marketplace down" alert
NAMES_REFRESH = 1800.0    # autocomplete catalogue
SESSION_CHECK = 3600.0    # token expiry warnings


def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_trigger(settings: Settings, listing: Listing, change: Change) -> bool:
    """Only a CHANGE is an opportunity: listed again, a cheaper (or different)
    price, more units at the price. A first sighting counts only with
    act_on_first_sight, or when the item just appeared while watched (a new
    release, marked by the adapter)."""
    if change in (Change.RESTOCKED, Change.PRICE_CHANGED, Change.MORE_STOCK):
        return True
    if change is Change.NEW:
        return settings.act_on_first_sight or bool(listing.extra.get("new_release"))
    return False


class Watcher:
    def __init__(self, settings: Settings, store: Store, adapter: MarketplaceAdapter,
                 notifier: DiscordNotifier):
        self.s = settings
        self.store = store
        self.adapter = adapter
        self.notifier = notifier
        self.limits = Limits(settings, store)
        self.engine = ClaimEngine(settings, store, adapter, notifier, self.limits)
        self.watchlist = WatchList(settings, store)
        self._stop = asyncio.Event()
        self._down_alerted: set[str] = set()
        self._tasks: list[asyncio.Task] = []
        self.started_at = _now()
        self.last_poll: datetime | None = None
        self.held: set[str] = set()

    def stop(self):
        self._stop.set()

    async def _sleep(self, seconds: float) -> bool:
        """Sleep unless asked to stop. Returns True if stopping."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(0.0, seconds))
            return True
        except asyncio.TimeoutError:
            return False

    def _markets(self) -> dict[str, MarketplaceAdapter]:
        adapters = getattr(self.adapter, "adapters", None)
        return dict(adapters) if adapters else {self.adapter.name: self.adapter}

    # ------------------------------------------------------------ one sweep

    async def process(self, listings: list[Listing],
                      complete: dict[str, set[str]] | None = None
                      ) -> list[tuple[Listing, str]]:
        """Evaluate a sweep. Returns [(listing, action)] for watched items acted on."""
        acted: list[tuple[Listing, str]] = []
        entries = self.watchlist.entries()   # once per sweep: Discord edits apply next poll
        # Off the event loop, so Discord commands stay responsive during a sweep.
        changes = await asyncio.to_thread(self.store.observe_many, listings, complete)
        can_claim = getattr(self.adapter, "can_claim", None)
        for l, change in zip(listings, changes):
            if not is_trigger(self.s, l, change):
                continue
            watch = WatchList.find(entries, l.name, l.slug, l.marketplace)
            if watch is None:
                continue
            ok, why = self.engine.eligible(l, watch)
            if not ok:
                log.info("%s on %s %s but skipped: %s", l.name, l.marketplace, change.value, why)
                continue
            log.info("watched item %s on %s %s: x%d at %s", l.name, l.marketplace,
                     change.value, l.quantity, l.price)
            if self.s.mode == "monitor" or (can_claim is not None and not can_claim(l)):
                # Monitor mode, or no session for that market: alert only.
                reason = "" if self.s.mode == "monitor" else (
                    f"Alert only: {self.adapter.claim_reason(l)}."
                    if hasattr(self.adapter, "claim_reason") else "")
                self.notifier.available(l, self.s.mode, reason)
                acted.append((l, "notified"))
                continue
            outcome = await self.engine.attempt(l, watch)
            acted.append((l, outcome.status.value))
            if outcome.status is ClaimStatus.CLAIMED and 0 < outcome.quantity < l.quantity:
                # Units left at this price (a per-buy cap stopped short): try again next
                # poll. The daily cap and the balance still stop it there.
                self.store.mark_unavailable(l.marketplace, l.listing_id)
            elif outcome.status is ClaimStatus.SKIPPED and not outcome.detail.startswith(
                    ("daily cap", "duplicate", "price", "revalidation: price", "balance",
                     "an unclear buy")):
                self.store.mark_unavailable(l.marketplace, l.listing_id)  # retry next poll
        return acted

    # ------------------------------------------------------------ background

    async def _balance_loop(self):
        """Keep every buying market's balance fresh, so a buy never waits on it."""
        while not self._stop.is_set():
            for name, a in self._markets().items():
                if a.capabilities.claim and name in self.watchlist.markets():
                    await self.engine.refresh_balance(name)
            if await self._sleep(self.s.balance_refresh_seconds):
                return

    async def _housekeeping_loop(self):
        """Autocomplete names now and then, and session expiry warnings."""
        next_names = next_session = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_names:
                next_names = now + NAMES_REFRESH
                refresh = getattr(self.adapter, "refresh_names", None)
                if refresh:
                    try:
                        await refresh()
                    except Exception as e:  # noqa: BLE001
                        log.debug("name refresh failed: %s", e)
            if now >= next_session:
                next_session = now + SESSION_CHECK
                for name, a in self._markets().items():
                    warning = a.session_warning()
                    if warning:
                        self.notifier.system(f"🔑 {label(name)} session", warning, "warn",
                                             key=f"session:{name}")
            if await self._sleep(60):
                return

    # ------------------------------------------------------------ main loop

    async def run(self):
        self.engine.reconcile_on_startup()
        await self.adapter.connect()
        markets = self._markets()
        claimless = [f"{label(n)}: {a.capabilities.claim_unavailable_reason}"
                     for n, a in markets.items()
                     if not a.capabilities.claim and n in self.watchlist.markets()]
        claimless += [f"{label(n)}: the session check failed (see /balance)"
                      for n, a in markets.items()
                      if a.capabilities.claim and not a.account
                      and n in self.watchlist.markets()]
        self.notifier.system(
            "🟢 MM2 claimer started",
            f"v{__version__} · mode **{self.s.mode}** · watching "
            f"{len(self.watchlist.entries())} item(s) every {self.s.poll_interval_seconds:g}s"
            + (" · Discord commands on" if self.watchlist.managed else "")
            + f" · markets: {', '.join(label(m) for m in sorted(self.watchlist.markets()))}"
            + ("" if self.s.mode == "monitor" or not claimless
               else "\n⚠️ Alert only on " + "; ".join(claimless)),
            "ok")
        self._tasks = [asyncio.create_task(self._balance_loop(), name="balances"),
                       asyncio.create_task(self._housekeeping_loop(), name="housekeeping")]
        # One loop per market: BloxyMarket's spaced, rate-limited requests never slow
        # BHOPP's polls down. Buys still go one at a time (the engine's lock).
        loops = [asyncio.create_task(self._market_loop(name), name=f"poll-{name}")
                 for name in self._markets()]
        try:
            await self._stop.wait()
        finally:
            for t in self._tasks + loops:
                t.cancel()
            await asyncio.gather(*self._tasks, *loops, return_exceptions=True)

    async def _market_loop(self, name: str):
        # Waits that must not shrink to 0 with POLL_INTERVAL_SECONDS=0 (errors, pause).
        idle = max(self.s.poll_interval_seconds, 1.0)
        while not self._stop.is_set():
            t0 = time.monotonic()
            wait = self.adapter.wait_time(name)
            if wait > 0:   # backing off after failures (the adapter tracks it)
                if await self._sleep(min(wait, idle * 10)):
                    return
                continue
            try:
                result = await self._tick({name})
            except Exception:  # noqa: BLE001 - a bug in one pass must not end the loop
                log.exception("%s poll pass failed", name)
                result = None
                await self._sleep(idle)
            if result is None:
                continue   # paused, held or switched off: _tick already waited
            gap = self.s.poll_interval_seconds * random.uniform(0.9, 1.1)
            if await self._sleep(gap - (time.monotonic() - t0)):
                return

    async def _tick(self, markets: set[str] | None = None) -> bool | None:
        """One pass over `markets` (default all). False = every polled market failed,
        None = nothing polled (paused, held, switched off; this already waited)."""
        markets = set(markets or self._markets())
        idle = max(self.s.poll_interval_seconds, 1.0)
        try:
            held = await self.engine.check_holds(markets)
        except StoreError as e:
            log.error("hold check failed: %s", e)
            held = set(markets)
        self.held = (self.held - markets) | held
        for name in markets:
            a = self._markets()[name]
            a.status.paused_reason = "checking an unclear buy" if name in held else None
        if self.watchlist.paused:
            for name in markets:
                self._markets()[name].status.paused_reason = "paused from Discord (/watch resume)"
            self.write_status()
            await self._sleep(idle)
            return None
        targets = {m for m in markets if m in self.watchlist.markets() and m not in held}
        if not targets:
            self.write_status()
            await self._sleep(self.s.claim_status_interval_seconds if held else idle)
            return None

        # Only watched items are parsed and recorded.
        keys = {w.key for w in self.watchlist.entries()}
        res = await self.adapter.poll(only=keys, markets=targets)
        self._poll_health(res)
        if res.all_failed:
            for e in res.failures:
                if not isinstance(e, (AuthError, RateLimited)):
                    log.debug("poll failed: %s", e)
            self.write_status()
            return False
        if res.complete:
            self.last_poll = _now()
        try:
            await self.process(res.listings, res.complete)
        except Exception:  # noqa: BLE001 - a bug in a sweep must not kill the service
            log.exception("error while processing listings")
        self.write_status()
        return True

    def _poll_health(self, res):
        """Per-market reachability, alerts on a market going down / coming back."""
        errors, failures = self.adapter.errors, self.adapter.failures
        limited = self.adapter.rate_limited
        for name in res.polled:
            st = self._markets()[name].status
            st.polls += 1
            if name in res.complete:
                st.listings_tracked = len(res.complete[name])
                if name in self._down_alerted:
                    self._down_alerted.discard(name)
                    self.notifier.system(f"🟢 {label(name)} reachable again",
                                         f"Recovered after {st.consecutive_failures} "
                                         "failed polls.", "ok")
                st.reachable, st.last_success, st.last_error = True, _now(), None
                st.consecutive_failures, st.rate_limited_until = 0, None
                continue
            st.consecutive_failures = failures.get(name, st.consecutive_failures + 1)
            st.last_error = errors.get(name)
            st.reachable = False
            if name in limited:
                st.rate_limited_until = _now() + timedelta(seconds=limited[name])
                self.notifier.system(f"Rate limited by {label(name)}",
                                     f"Pausing {label(name)} for {limited[name]:.0f}s, "
                                     "then resuming. The other market keeps going.",
                                     "warn", key=f"ratelimit:{name}")
            if st.consecutive_failures >= DOWN_AFTER and name not in self._down_alerted:
                self._down_alerted.add(name)
                self.notifier.system(f"🔴 {label(name)} unreachable",
                                     f"{st.consecutive_failures} failed polls in a row "
                                     f"(latest: {st.last_error}). Still retrying.", "error")

    # ------------------------------------------------------------ status

    def status_dict(self) -> dict:
        holds, spent = [], None
        try:
            holds = [{"market": h.market, "item": h.name, "since": h.since.isoformat(),
                      "until": h.until.isoformat() if h.until else None}
                     for h in self.store.holds()]
            spent = str(self.store.spent(utc_day()))
        except StoreError:
            pass
        return {
            "version": __version__,
            "updated_at": _now().isoformat(),
            "started_at": self.started_at.isoformat(),
            "mode": self.s.mode,
            "holds": holds,
            "spent_today": spent,
            "balances": {m: str(v) for m, (v, _) in self.engine.balances.items()},
            "markets": {n: a.status.as_dict() for n, a in self._markets().items()},
            "discord": {"enabled": self.notifier.enabled, "sent": self.notifier.sent,
                        "failed": self.notifier.failed, "dropped": self.notifier.dropped},
        }

    def write_status(self):
        try:
            path = self.s.status_file
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.status_dict(), indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError as e:
            log.debug("could not write status file: %s", e)


def build_http(settings: Settings) -> HttpClient:
    # Short spacing for BHOPP: a buy makes 2-3 requests back to back and must not
    # queue behind the poll. BloxyMarket rate-limits by IP, so it gets a per-minute
    # budget that lets one poll's searches go out as a quick burst.
    return HttpClient(min_host_interval=0.2, default_timeout=settings.request_timeout_seconds,
                      host_limits={"api.bloxymarket.com":
                                   HostLimit(settings.bloxymarket_requests_per_minute)})


def build_adapter(settings: Settings, http: HttpClient, enabled=None) -> MarketplaceAdapter:
    """BHOPP and BloxyMarket, polled as `enabled()` says."""
    from .marketplaces.bhopp import BhoppAdapter
    from .marketplaces.bloxymarket import BloxyMarketAdapter
    from .marketplaces.multi import MultiMarketAdapter
    bhopp = BhoppAdapter(http, token=settings.bhopp_token,
                         timeout=settings.request_timeout_seconds,
                         search_limit=settings.search_query_limit)
    bloxy = BloxyMarketAdapter(http, cookie=settings.bloxymarket_cookie,
                               timeout=settings.request_timeout_seconds,
                               search_limit=settings.search_query_limit,
                               session_path=settings.data_dir / "bloxymarket_session.json")
    return MultiMarketAdapter({"bhopp": bhopp, "bloxymarket": bloxy},
                              enabled or (lambda: set(settings.markets)),
                              base_interval=settings.poll_interval_seconds)


async def run_service(settings: Settings):
    # One instance per DATA_DIR: two would keep separate ledgers and both buy.
    # (Also run only one deployment per account.)
    lock = InstanceLock(settings.data_dir / "mm2claimer.lock")
    if not lock.acquire():
        raise SystemExit(f"another mm2claimer is already running with DATA_DIR="
                         f"{settings.data_dir}; stop it first")
    try:
        await _run_service(settings)
    finally:
        lock.release()


async def _run_service(settings: Settings):
    store = Store(settings.db_path)
    http = build_http(settings)
    holder = {}
    adapter = build_adapter(settings, http, lambda: holder["w"].watchlist.markets())
    notifier = DiscordNotifier(settings.discord_webhook_url, username=settings.discord_username,
                               cooldown_seconds=settings.alert_cooldown_seconds,
                               mention=settings.discord_mention)
    notifier.start()
    watcher = Watcher(settings, store, adapter, notifier)
    holder["w"] = watcher
    bot = None
    if settings.discord_bot_token:
        from .discord_bot import Commands, start_bot
        bot = await start_bot(settings, Commands(settings, store, adapter, watcher), notifier)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, watcher.stop)
        except (NotImplementedError, RuntimeError):  # Windows
            pass
    log.info("starting: %s", settings.redacted())
    try:
        await watcher.run()
    finally:
        log.info("shutting down")
        notifier.system("⏹️ MM2 claimer stopped", "Service shut down.", "info")
        watcher.write_status()
        if bot is not None:
            await bot.close()
        await notifier.stop()
        await adapter.close()
        await http.close()
        store.close()
