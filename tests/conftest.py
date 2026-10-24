import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from mm2claimer.config import load_settings
from mm2claimer.http import HttpResult
from mm2claimer.marketplaces.base import MarketplaceAdapter
from mm2claimer.models import Capabilities, ClaimOutcome, ClaimStatus, Listing
from mm2claimer.store import Store

D = Decimal


def run(coro):
    return asyncio.run(coro)


def make_settings(tmp_path, mode="monitor", extra_env=None, watch="Harvester<=6, Chroma Seer<=1"):
    env = {"MODE": mode, "DATA_DIR": str(tmp_path / "data"), "LOG_DIR": str(tmp_path / "logs"),
           "WATCH": watch}
    if mode == "claim":
        env["DISCORD_WEBHOOK_URL"] = "https://discord.com/api/webhooks/1/abc-DEF_123"
        env["BHOPP_TOKEN"] = "test-token-not-real"
        env["BLOXYMARKET_COOKIE"] = "sid=test-cookie-not-real"
    env.update(extra_env or {})
    return load_settings(None, env=env)


def listing(market="bhopp", name="Harvester", price="5.5", qty=2, lid=None, **extra):
    lid = lid or name.lower().replace(" ", "-")
    return Listing(marketplace=market, listing_id=lid, slug=lid, name=name, price=D(price),
                   unit="USD", available=qty > 0, quantity=qty,
                   url=f"https://example.test/{lid}", extra=dict(extra))


class FakeNotifier:
    enabled = True

    def __init__(self):
        self.events = []
        self.sent = self.failed = self.dropped = 0

    def claim_succeeded(self, listing, outcome):
        self.events.append(("claimed", listing.listing_id, outcome))

    def claim_failed(self, listing, outcome):
        self.events.append(("failed", listing.listing_id, outcome))

    def available(self, listing, mode, reason=""):
        self.events.append(("available", listing.listing_id, reason))

    def system(self, title, message, level="info", key=None):
        self.events.append(("system", title, message))

    def kinds(self):
        return [e[0] for e in self.events]

    def titles(self):
        return [e[1] for e in self.events if e[0] == "system"]


class FakeAdapter(MarketplaceAdapter):
    """Scriptable single-market adapter: serves fixed listings, records buys."""

    def __init__(self, name="bhopp", ls=None, *, outcomes=None, balance=D("100"), claim=True,
                 details=None, reconcile=None, claim_delay=0.0, claim_exc=None):
        self.name = name
        self.capabilities = Capabilities(claim=claim,
                                         claim_unavailable_reason="" if claim else "no token")
        super().__init__()
        self.ls = list(ls or [])
        self.calls = []
        self.outcomes = list(outcomes or [])
        self.balance = balance
        self.balance_reads = 0
        self.details = details          # None: echo the listing; callable(listing) or value
        self.reconcile = list(reconcile or [])
        self.claim_delay = claim_delay
        self.claim_exc = list(claim_exc or [])
        self.poll_exc = []
        self.account = "TestBuyer"

    async def get_listings(self, only=None):
        if self.poll_exc:
            raise self.poll_exc.pop(0)
        # Like the real adapters: every poll is a new sighting.
        now = datetime.now(timezone.utc)
        return [replace(l, detected_at=now) for l in self.ls
                if only is None or l.listing_id in only or l.name.lower() in only]

    async def get_listing_details(self, listing):
        if callable(self.details):
            return self.details(listing)
        return listing if self.details is None else self.details

    async def get_balance(self):
        self.balance_reads += 1
        if isinstance(self.balance, Exception):
            raise self.balance
        return self.balance

    async def read_account(self):
        return self.account

    async def claim_listing(self, listing, idem_key):
        self.calls.append((listing, idem_key))
        if self.claim_delay:
            await asyncio.sleep(self.claim_delay)
        if self.claim_exc:
            raise self.claim_exc.pop(0)
        if self.outcomes:
            return self.outcomes.pop(0)
        return ClaimOutcome(ClaimStatus.CLAIMED, "order-1", "ok", quantity=listing.quantity,
                            charged=listing.price * listing.quantity, username=self.account)

    async def reconcile_claim(self, listing):
        return self.reconcile.pop(0) if self.reconcile else (ClaimStatus.UNKNOWN, None)


class FakeHttp:
    """Answers requests from routes: [(method, url_part, answer)], first match wins and is
    used once (unless the answer is wrapped in `Always`). An Exception answer is raised;
    a callable is called with the request and its result used."""

    def __init__(self, *routes):
        self.routes = list(routes)
        self.calls = []

    async def request_json(self, method, url, *, json_body=None, params=None, headers=None,
                           timeout=None, etag=None, spaced=True):
        call = {"method": method, "url": url, "json": json_body, "params": params or {},
                "headers": headers or {}, "spaced": spaced}
        self.calls.append(call)
        for i, (m, part, ans) in enumerate(self.routes):
            if m == method and part in url:
                if not isinstance(ans, Always):
                    self.routes.pop(i)
                else:
                    ans = ans.answer
                if callable(ans) and not isinstance(ans, Exception):
                    ans = ans(call)
                if isinstance(ans, Exception):
                    raise ans
                return ans if isinstance(ans, HttpResult) else HttpResult(200, ans)
        raise AssertionError(f"unexpected request {method} {url}")

    def posts(self):
        return [c for c in self.calls if c["method"] == "POST"]


class Always:
    def __init__(self, answer):
        self.answer = answer


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()
