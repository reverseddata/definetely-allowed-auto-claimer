"""BloxyMarket adapter (bloxymarket.com, API at api.bloxymarket.com).

Monitoring (verified live 2026-10-25, public, no login):

    GET /v1/marketplace?page=N&page_size=50&game_type=mm2&sort=low_to_high[&display_name=T]
        {"message": "success", "total_items", "results": [item...]}; pages start at 0,
        50 per page is the cap, the only sorts are low_to_high / high_to_low.
        item: {game_name, display_name, thumbnail, item_type, rarity, chroma,
               listings: [{"_id", "price"}]}  -- one entry per unit for sale
        `display_name` is a case-insensitive substring filter. Prices are coins,
        1 coin = $1.00.

    One listing per item: its cheapest price, quantity = units at that price, and
    their ids (a buy names each unit). Deeper price levels are bought on later
    polls, when the cheapest price rises to them.

    The API rate-limits by IP over a sustained window (429 until it cools down), so
    requests to it are spaced (BLOXYMARKET_REQUEST_GAP_SECONDS) and a 429 backs this
    market off on its own.

Buying (read from the site's JavaScript 2026-10-25; not yet run against a live
account, so the first real buy is the test):

    GET  /authenticate   {"message": "success", "username", "email", ...}
    GET  /v1/balance     {"message": "success", "balance": {"total", "withdrawable"}}
    POST /v1/purchase    {"items": [{"item_id", "expected_price"}]} -> {"message": "success"}
                         anything else in "message" is the refusal, in the site's words.
                         expected_price makes the site refuse a unit whose price moved.
    GET  /v2/inventory?page=0&page_size=25&filter_type=all&sort_order=desc
                       &display_name=NAME&game_type=mm2
                         {"message": "success", "items": [{display_name, listed_ids,
                          unlisted_ids}]} -- where a bought unit shows up (reconcile)

    A purchase is final and lands in your BloxyMarket inventory; withdrawing it to
    Roblox is left to you on the site.

Auth is the site's login cookies. BLOXYMARKET_COOKIE holds the Cookie header value
of a logged-in tab. Cookies the API sets on any answer are kept and saved to
DATA_DIR, so a rotated session survives a restart. The site has no refresh call: when
the login itself ends, BloxyMarket answers 401 and the bot asks for a fresh paste.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

from ..http import (AuthError, BadResponse, HttpClient, MarketplaceError, RateLimited,
                    RequestNotSent, TransientError)
from ..models import (Capabilities, ClaimOutcome, ClaimStatus, Listing, norm_name, parse_price,
                      search_term)
from ..notify import register_secret
from .base import MarketplaceAdapter

log = logging.getLogger(__name__)

API_URL = "https://api.bloxymarket.com"
MARKET_URL = f"{API_URL}/v1/marketplace"
PURCHASE_URL = f"{API_URL}/v1/purchase"
BALANCE_URL = f"{API_URL}/v1/balance"
AUTH_URL = f"{API_URL}/authenticate"
INVENTORY_URL = f"{API_URL}/v2/inventory"
SITE = "https://bloxymarket.com"
HEADERS = {"Origin": SITE, "Referer": f"{SITE}/"}
UNIT = "USD"
PAGE_SIZE = 50
MAX_PAGES = 20
_NO_MONEY = ("balance", "insufficient", "funds", "afford", "enough")


def item_url(unit_id: str) -> str:
    return f"{SITE}/market/item/{quote(unit_id, safe='')}" if unit_id else f"{SITE}/market"


def display_name(row: dict) -> str:
    name = str(row.get("display_name") or row.get("game_name") or "").strip()
    if row.get("chroma") and not name.lower().startswith("chroma"):
        name = f"Chroma {name}"
    return name


def row_keys(row) -> set[str]:
    if not isinstance(row, dict):
        return set()
    return {norm_name(display_name(row)), norm_name(str(row.get("game_name") or ""))}


def normalize_rows(rows) -> tuple[list[Listing], int]:
    """/v1/marketplace results -> (listings, malformed_count)."""
    if not isinstance(rows, list):
        raise BadResponse("unexpected /v1/marketplace payload shape")
    out: list[Listing] = []
    bad = 0
    seen: set[str] = set()
    for row in rows:
        try:
            name = display_name(row)
            lid = str(row.get("game_name") or name).strip()
            if not name or not lid or lid in seen:
                bad += not name
                continue
            units = []
            for u in row.get("listings") or []:
                price = parse_price(u.get("price"))
                if price is not None and price > 0 and u.get("_id"):
                    units.append((price, str(u["_id"])))
        except (TypeError, AttributeError):
            bad += 1
            continue
        seen.add(lid)
        if not units:
            continue
        units.sort()
        best = units[0][0]
        ids = [i for p, i in units if p == best]
        rarity = str(row.get("rarity") or "").strip()
        out.append(Listing(
            marketplace="bloxymarket", listing_id=lid, slug=lid, name=name, price=best,
            unit=UNIT, available=True, quantity=len(ids),
            rarity=None if rarity.lower() in ("", "unknown") else rarity,
            chroma=bool(row.get("chroma")),
            item_type=(row.get("item_type") if row.get("item_type") != "Unknown" else None),
            url=item_url(ids[0]), image=str(row.get("thumbnail") or ""),
            extra={"ids": ids}))
    return out, bad


def parse_cookie_header(header: str) -> dict[str, str]:
    """"a=1; b=2" -> {"a": "1", "b": "2"}. Values are kept exactly as copied."""
    jar: dict[str, str] = {}
    for part in header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            jar[name] = value
    return jar


def cookie_header(jar: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in jar.items())


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _success(data) -> bool:
    return isinstance(data, dict) and str(data.get("message") or "").lower() == "success"


def _message(data) -> str:
    if isinstance(data, dict):
        for k in ("message", "error", "detail"):
            if data.get(k):
                return " ".join(str(data[k]).split())[:200]
    return "no message"


class BloxyMarketAdapter(MarketplaceAdapter):
    name = "bloxymarket"
    unit = UNIT
    # The purchase names each unit with its expected price, so the site itself
    # refuses a unit that sold or changed price, atomically. A separate re-check would
    # only add one rate-limited request (seconds) between seeing the item and buying.
    server_checks_price = True

    def __init__(self, http: HttpClient, *, cookie: str = "", timeout: float = 15.0,
                 search_limit: int = 6, session_path: Path | None = None):
        self.cookie = cookie.strip()
        self.capabilities = Capabilities(
            monitoring=True, claim=bool(self.cookie),
            claim_unavailable_reason="" if self.cookie else
            "buying on BloxyMarket is not configured (BLOXYMARKET_COOKIE not set)")
        super().__init__()
        self.http = http
        self.timeout = timeout
        self.search_limit = search_limit
        self.jar = parse_cookie_header(self.cookie)
        self._session_path = session_path
        self._names: set[str] = set()
        self._prev_only: set[str] | None = None
        self._prev_present: set[str] = set()
        self._load_session()
        # Every cookie value is a secret: the redactor scrubs them exactly, in any form.
        register_secret(self.cookie)
        for value in self.jar.values():
            register_secret(value)

    # ------------------------------------------------------------ session

    def _load_session(self):
        """Use cookies the API rotated in an earlier run, but only if they belong to
        the cookie in .env now. A new paste in .env always wins."""
        if self._session_path is None or not self.cookie:
            return
        try:
            saved = json.loads(self._session_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(saved, dict) and saved.get("token_sha") == _fingerprint(self.cookie):
            cookies = saved.get("cookies")
            if isinstance(cookies, dict):
                self.jar.update({str(k): str(v) for k, v in cookies.items()})

    def _save_session(self):
        if self._session_path is None:
            return
        try:
            self._session_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._session_path.with_suffix(".tmp")
            # Created owner-only from the start (no window where it is world-readable).
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps({"token_sha": _fingerprint(self.cookie), "cookies": self.jar}))
            tmp.replace(self._session_path)
        except OSError as e:
            log.warning("could not save the BloxyMarket session: %s", e)

    async def connect(self):
        """Read the account once at startup, so the first buy doesn't wait on it and a
        dead session shows up in the logs right away."""
        if self.cookie:
            try:
                await self.read_account()
                log.info("BloxyMarket session OK: %s", self.account)
            except MarketplaceError as e:
                log.warning("BloxyMarket session check failed: %s", e)

    async def _authed(self, method: str, url: str, json_body=None, params=None,
                      spaced: bool = True):
        kw = {} if spaced else {"spaced": False}
        res = await self.http.request_json(
            method, url, json_body=json_body, params=params,
            headers={**HEADERS, "Cookie": cookie_header(self.jar)}, timeout=self.timeout, **kw)
        rotated = {k: v for k, v in (res.cookies or {}).items() if v and self.jar.get(k) != v}
        if rotated:
            for value in rotated.values():
                register_secret(value)
            self.jar.update(rotated)
            self._save_session()
            log.info("BloxyMarket session cookies updated")
        return res

    async def read_account(self) -> str:
        """Username of the logged-in session (GET /authenticate). Read once, then cached."""
        if self.account:
            return self.account
        res = await self._authed("GET", AUTH_URL)
        if not _success(res.data):
            raise AuthError(f"BloxyMarket says the session is not logged in "
                            f"({_message(res.data)})")
        name = str(res.data.get("username") or "").strip()
        if not name:
            raise BadResponse("BloxyMarket answered without a username")
        self.account = name
        return name

    async def get_balance(self) -> Decimal:
        """Balance in dollars (GET /v1/balance, balance.total). Read-only."""
        res = await self._authed("GET", BALANCE_URL)
        if not _success(res.data):
            raise BadResponse(f"BloxyMarket balance: {_message(res.data)}")
        bal = res.data.get("balance")
        value = parse_price(bal.get("total") if isinstance(bal, dict) else None)
        if value is None:
            raise BadResponse("BloxyMarket answered without a balance")
        return value

    # ------------------------------------------------------------ monitoring

    async def _page(self, page: int, term: str = "") -> dict:
        params = {"page": page, "page_size": PAGE_SIZE, "game_type": "mm2",
                  "sort": "low_to_high"}
        if term:
            params["display_name"] = term
        res = await self.http.request_json("GET", MARKET_URL, params=params, headers=HEADERS,
                                           timeout=self.timeout)
        if not _success(res.data) or not isinstance(res.data.get("results"), list):
            raise BadResponse(f"unexpected /v1/marketplace answer: {_message(res.data)}")
        return res.data

    async def _sweep(self, term: str = "") -> list:
        """Every page, one after the other (the API rate-limits bursts). Raises unless
        every page arrived."""
        rows: list = []
        for page in range(MAX_PAGES):
            data = await self._page(page, term)
            batch = data["results"]
            rows.extend(batch)
            total = data.get("total_items")
            if len(batch) < PAGE_SIZE or (isinstance(total, int) and len(rows) >= total):
                return rows
        return rows

    async def _rows(self, only: set[str] | None) -> list:
        terms = {search_term(k) for k in only or ()} - {""}
        if only is not None and len(terms) <= self.search_limit:
            rows, seen = [], set()
            for t in sorted(terms):
                for r in await self._sweep(t):
                    key = r.get("game_name") if isinstance(r, dict) else None
                    if key not in seen:
                        seen.add(key)
                        rows.append(r)
            return rows
        return await self._sweep()

    async def get_listings(self, only: set[str] | None = None) -> list[Listing]:
        rows = await self._rows(only)
        if only is not None:
            rows = [r for r in rows if row_keys(r) & only]
        listings, bad = normalize_rows(rows)
        self._names.update(l.name for l in listings)
        if bad:
            log.debug("bloxymarket: skipped %d malformed rows", bad)
        if only is not None:
            self._mark_new(only, listings)
        return listings

    def _mark_new(self, only: set[str], listings: list[Listing]):
        """A watched item missing on the last poll and listed now counts even on its
        first sighting (a new release, or back on the market)."""
        present = set()
        for l in listings:
            keys = {norm_name(l.name), norm_name(l.slug)} & only
            present |= keys
            if self._prev_only is not None and keys & (self._prev_only - self._prev_present):
                l.extra["new_release"] = True
        self._prev_only, self._prev_present = set(only), present

    async def lookup(self, item: str) -> list[Listing]:
        want = norm_name(item)
        term = search_term(want)
        if not term:
            return []
        listings, _ = normalize_rows(await self._sweep(term))
        self._names.update(l.name for l in listings)
        return [l for l in listings if want in norm_name(l.name) or want == norm_name(l.slug)]

    async def get_listing_details(self, listing: Listing) -> Listing | None:
        rows = await self._sweep(search_term(norm_name(listing.name)))
        return next((l for l in normalize_rows(rows)[0] if l.listing_id == listing.listing_id),
                    None)

    def item_names(self) -> list[str]:
        """Names seen in polls and lookups. No sweep of its own: at one request per
        few seconds, a full read of the market would stall polling for half a minute.
        BHOPP's catalogue already names every MM2 item."""
        return sorted(self._names)

    # ------------------------------------------------------------ buying

    async def claim_listing(self, listing: Listing, idem_key: str) -> ClaimOutcome:
        """POST /v1/purchase for listing.quantity of the cheapest units, each with its
        expected price, so the site refuses rather than charge more."""
        ids = list(listing.extra.get("ids") or [])[:max(1, listing.quantity)]
        if not self.cookie:
            return ClaimOutcome(ClaimStatus.SKIPPED,
                                detail=self.capabilities.claim_unavailable_reason)
        if not ids:
            return ClaimOutcome(ClaimStatus.REJECTED, detail="listing has no unit ids")
        try:
            await self.read_account()
        except RequestNotSent:
            raise
        except (AuthError, BadResponse) as e:
            return ClaimOutcome(ClaimStatus.REJECTED, detail=f"BloxyMarket session refused: "
                                f"{e}. Paste a fresh BLOXYMARKET_COOKIE.")
        except MarketplaceError as e:
            return ClaimOutcome(ClaimStatus.SKIPPED, detail=f"BloxyMarket unreachable, "
                                                            f"nothing bought: {e}")
        body = {"items": [{"item_id": i, "expected_price": float(listing.price)} for i in ids]}
        try:
            res = await self._authed("POST", PURCHASE_URL, json_body=body, spaced=False)
        except RequestNotSent:
            raise
        except AuthError as e:
            return ClaimOutcome(ClaimStatus.REJECTED, detail=f"BloxyMarket refused the session "
                                f"({e}). Paste a fresh BLOXYMARKET_COOKIE.")
        except RateLimited as e:
            return ClaimOutcome(ClaimStatus.REJECTED, detail=f"BloxyMarket refused: {e}")
        except BadResponse as e:
            if e.status is None or e.status < 400:
                return ClaimOutcome(ClaimStatus.UNKNOWN, detail=f"unreadable answer: {e}")
            return ClaimOutcome(ClaimStatus.REJECTED, detail=f"BloxyMarket refused: {e}",
                                no_balance=any(w in str(e).lower() for w in _NO_MONEY))
        except TransientError as e:
            return ClaimOutcome(ClaimStatus.UNKNOWN, detail=f"no clear answer: {e}")

        if not _success(res.data):
            msg = _message(res.data)
            if not isinstance(res.data, dict) or msg == "no message":
                return ClaimOutcome(ClaimStatus.UNKNOWN, detail="purchase answered without "
                                                                "a result")
            return ClaimOutcome(ClaimStatus.REJECTED, detail=f"BloxyMarket refused: {msg}",
                                no_balance=any(w in msg.lower() for w in _NO_MONEY))
        claim_id = ids[0] if len(ids) == 1 else f"{ids[0]} (+{len(ids) - 1})"
        return ClaimOutcome(
            ClaimStatus.CLAIMED, claim_id=claim_id, detail=f"bought {len(ids)} unit(s)",
            link=f"{SITE}/inventory", username=self.account, charged=listing.price * len(ids),
            quantity=len(ids),
            note="It is in your BloxyMarket inventory. Withdraw it there when you want it "
                 "in Roblox.")

    async def reconcile_claim(self, listing: Listing) -> tuple[ClaimStatus, str | None]:
        """Look for the units in our inventory. Not there is NOT proof of no buy,
        so it stays UNKNOWN."""
        if not self.cookie:
            return ClaimStatus.UNKNOWN, None
        res = await self._authed("GET", INVENTORY_URL, params={
            "page": 0, "page_size": 25, "filter_type": "all", "sort_order": "desc",
            "display_name": listing.name, "game_type": "mm2"})
        if not _success(res.data):
            return ClaimStatus.UNKNOWN, None
        want = set(listing.extra.get("ids") or [])
        for item in res.data.get("items") or []:
            if not isinstance(item, dict):
                continue
            owned = set(item.get("listed_ids") or []) | set(item.get("unlisted_ids") or [])
            got = want & owned
            if got:
                return ClaimStatus.CLAIMED, sorted(got)[0]
        return ClaimStatus.UNKNOWN, None
