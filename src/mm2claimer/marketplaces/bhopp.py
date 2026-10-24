"""BHOPP adapter (bhopp.com, API at api.sublimelistings.com:5000).

Monitoring (verified live 2026-10-25, public, no login, plain HTTP client works):

    GET /market?limit=100&page=N[&search=TERM]
        {"items": [product...], "page", "pages", "total"}; 100 per page is the cap.
        product: {id, slug, name, avatar, rarity, category, createdAt,
                  offers: [{id, amount, currency, quantity, seller: {id, username}}]}
        Only products with at least one offer are listed. `search` is a
        case-insensitive substring of the name. No ETag revalidation (always 200).
    GET /products/{slug}/offers   {"offers": [...]} for one product (the re-check)
    GET /products?limit=100&page=N   the whole catalogue, listed or not (names)

    Offer `amount` is in CENTS (140 = $1.40). One listing per product: its cheapest
    USD offer that is not our own; quantity = that offer's quantity.

Buying (read from the site's JavaScript 2026-10-25; not yet run against a live
account, so the first real buy is the test):

    GET   /connect          {authenticated, user: {id, username, wallet: {balance, locked}}}
                            wallet values are 1/10000 of a dollar
    POST  /orders/checkout  {"items": [{"offerId", "quantity"}]} -> {"orders": [order]}
          order: {id, state, seller, items: [{offerId, quantity, unitAmount}],
                  totals: {gross, fee, net}, conversationId}; totals in 1/10000 dollar,
                  unitAmount in cents. Refusals: {"errors": [...], "code"}, e.g.
                  code "insufficient_balance".
    GET   /orders?state=open&limit=10   {"orders": [...], "counts"} (reconcile)

    The order is paid at checkout and goes to the seller, who trades the item to
    you in Roblox; you then confirm receipt on bhopp.com. The bot does not wait.

Auth is `Authorization: Bearer <token>`: localStorage["bhopp.token"] on bhopp.com, a
JWT valid 7 days that the site never refreshes. So there is nothing to renew here:
the bot warns a day before it expires and when BHOPP rejects it.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import quote

from ..http import (AuthError, BadResponse, HttpClient, MarketplaceError, RateLimited,
                    RequestNotSent, TransientError)
from ..models import (Capabilities, ClaimOutcome, ClaimStatus, Listing, norm_name, parse_price,
                      search_term)
from ..notify import register_secret
from .base import MarketplaceAdapter

log = logging.getLogger(__name__)

API_URL = "https://api.sublimelistings.com:5000"
MARKET_URL = f"{API_URL}/market"
PRODUCTS_URL = f"{API_URL}/products"
CONNECT_URL = f"{API_URL}/connect"
CHECKOUT_URL = f"{API_URL}/orders/checkout"
ORDERS_URL = f"{API_URL}/orders"
SITE = "https://bhopp.com"
HEADERS = {"Origin": SITE, "Referer": f"{SITE}/"}
UNIT = "USD"
PAGE_SIZE = 100            # server maximum (101+ is a 422)
MAX_PAGES = 30
CENTS = Decimal(100)
WALLET = Decimal(10000)    # wallet and order totals: 1/10000 dollar
EXPIRY_WARNING = 24 * 3600
RECONCILE_WINDOW = 15 * 60  # an open order this recent, for our offer, is ours


def product_url(slug: str) -> str:
    return f"{SITE}/products/{quote(slug, safe='')}" if slug else f"{SITE}/market"


def chat_url(conversation_id) -> str:
    cid = str(conversation_id or "")
    return f"{SITE}/account/messages/{quote(cid, safe='')}" if cid else f"{SITE}/account"


def row_keys(row) -> set[str]:
    """Watch keys a /market product answers to (its name and its slug)."""
    if not isinstance(row, dict):
        return set()
    return {norm_name(str(row.get("name") or "")), norm_name(str(row.get("slug") or ""))}


def best_offer(offers, own_id: str = "") -> tuple[dict, Decimal] | None:
    """The cheapest USD offer we could buy: (offer, dollars per unit)."""
    best = None
    for o in offers or []:
        try:
            if (o.get("currency") or "USD").upper() != "USD":
                continue
            if own_id and str((o.get("seller") or {}).get("id") or "") == own_id:
                continue  # never buy our own offer
            cents = parse_price(o.get("amount"))
            qty = int(o.get("quantity") or 0)
            if cents is None or cents <= 0 or qty <= 0 or not o.get("id"):
                continue
        except (AttributeError, TypeError, ValueError, OverflowError):
            continue
        price = cents / CENTS
        if best is None or price < best[1]:
            best = (o, price)
    return best


def _listing(p: dict, offer: dict, price: Decimal) -> Listing:
    name = str(p.get("name") or p.get("slug") or "").strip()
    rarity = (str(p.get("rarity") or "").strip().lower() or None)
    seller = offer.get("seller") or {}
    return Listing(
        marketplace="bhopp", listing_id=str(p["slug"]), slug=str(p["slug"]), name=name,
        price=price, unit=UNIT, available=True, quantity=int(offer.get("quantity") or 0),
        # BHOPP tags chromas with rarity "chroma" (a marker, not a tier) and mis-tags a
        # few plain godlies with it; the name is what decides.
        rarity=None if rarity == "chroma" else rarity,
        chroma=name.lower().startswith("chroma "),
        item_type=p.get("category") or None, url=product_url(str(p["slug"])),
        image=str(p.get("avatar") or ""),
        extra={"offer_id": str(offer["id"]), "product_id": str(p.get("id") or ""),
               "seller": str(seller.get("username") or ""),
               "seller_id": str(seller.get("id") or "")})


def normalize_products(items, own_id: str = "") -> tuple[list[Listing], int]:
    """/market products -> (listings, malformed_row_count). Raises BadResponse if the
    payload as a whole has the wrong shape."""
    if not isinstance(items, list):
        raise BadResponse("unexpected /market payload shape")
    out: list[Listing] = []
    bad = 0
    seen: set[str] = set()
    for p in items:
        try:
            slug = str(p["slug"]).strip()
            if not slug or slug in seen:
                bad += not slug
                continue
            best = best_offer(p.get("offers"), own_id)
        except (KeyError, TypeError, AttributeError):
            bad += 1
            continue
        seen.add(slug)
        if best is not None:   # nothing buyable (only our own offers): not listed for us
            out.append(_listing(p, *best))
    return out, bad


def jwt_claims(token: str) -> dict:
    """The (unverified) payload of a JWT, {} if it isn't one."""
    try:
        part = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return data if isinstance(data, dict) else {}
    except (IndexError, ValueError):
        return {}


class BhoppAdapter(MarketplaceAdapter):
    name = "bhopp"
    unit = UNIT

    def __init__(self, http: HttpClient, *, token: str = "", timeout: float = 15.0,
                 search_limit: int = 6):
        self.token = token.strip()
        self.capabilities = Capabilities(
            monitoring=True, claim=bool(self.token),
            claim_unavailable_reason="" if self.token else
            "buying on BHOPP is not configured (BHOPP_TOKEN not set)")
        super().__init__()
        self.http = http
        self.timeout = timeout
        self.search_limit = search_limit
        claims = jwt_claims(self.token)
        self.user_id = str(claims.get("user") or claims.get("sub") or "")
        self.expires_at = claims.get("exp") if isinstance(claims.get("exp"), (int, float)) \
            else None
        self._names: list[str] = []
        self._prev_only: set[str] | None = None
        self._prev_present: set[str] = set()
        register_secret(self.token)

    # ------------------------------------------------------------ session

    async def connect(self):
        """Read the account once at startup, so the first buy doesn't wait on it and a
        dead token shows up in the logs right away."""
        if self.token:
            try:
                await self._me()
                log.info("BHOPP session OK: %s", self.account)
            except MarketplaceError as e:
                log.warning("BHOPP session check failed: %s", e)

    def _auth_headers(self) -> dict[str, str]:
        return {**HEADERS, "Authorization": f"Bearer {self.token}"}

    async def _authed(self, method: str, url: str, json_body=None, params=None):
        return await self.http.request_json(method, url, json_body=json_body, params=params,
                                            headers=self._auth_headers(),
                                            timeout=self.timeout)

    async def _me(self) -> dict:
        res = await self._authed("GET", CONNECT_URL)
        data = res.data if isinstance(res.data, dict) else {}
        user = data.get("user")
        if not data.get("authenticated") or not isinstance(user, dict):
            # /connect answers 200 for anyone; a dead token just isn't logged in.
            raise AuthError("BHOPP says the token is not logged in")
        if user.get("id"):
            self.user_id = str(user["id"])
        name = str(user.get("username") or "").strip()
        if name:
            self.account = name
        return user

    async def read_account(self) -> str:
        """The BHOPP username of the token's account. Read once, then cached."""
        if not self.account:
            await self._me()
        if not self.account:
            raise BadResponse("BHOPP answered without a username")
        return self.account

    async def get_balance(self) -> Decimal:
        """Spendable wallet balance in dollars (GET /connect). Read-only."""
        user = await self._me()
        wallet = user.get("wallet") if isinstance(user.get("wallet"), dict) else {}
        raw = parse_price(wallet.get("balance"))
        if raw is None:
            raise BadResponse("BHOPP answered without a wallet balance")
        return raw / WALLET

    def session_warning(self) -> str | None:
        if not self.token or not self.expires_at:
            return None
        left = self.expires_at - time.time()
        if left <= 0:
            return "Your BHOPP_TOKEN has expired. Log in on bhopp.com and paste a fresh one."
        if left <= EXPIRY_WARNING:
            when = datetime.fromtimestamp(self.expires_at, timezone.utc)
            return (f"Your BHOPP_TOKEN expires <t:{int(when.timestamp())}:R>. Paste a fresh "
                    "one before then (BHOPP tokens last 7 days and are never renewed).")
        return None

    # ------------------------------------------------------------ monitoring

    async def _get(self, url: str, params: dict) -> dict:
        res = await self.http.request_json("GET", url, params=params, headers=HEADERS,
                                           timeout=self.timeout)
        if not isinstance(res.data, dict):
            raise BadResponse(f"unexpected answer from {url}")
        return res.data

    async def _sweep(self, url: str, key: str, **params) -> list:
        """Every page of a paginated endpoint. Page 1 says how many there are, the
        rest go out together. Raises unless every page arrived."""
        first = await self._get(url, {"limit": PAGE_SIZE, "page": 1, **params})
        rows = first.get(key)
        if not isinstance(rows, list):
            raise BadResponse(f"unexpected {url} payload shape")
        rows = list(rows)
        pages = min(int(first.get("pages") or 1), MAX_PAGES)
        if pages > 1:
            rest = await asyncio.gather(*(self._get(url, {"limit": PAGE_SIZE, "page": p,
                                                          **params})
                                          for p in range(2, pages + 1)))
            for d in rest:
                more = d.get(key)
                if not isinstance(more, list):
                    raise BadResponse(f"unexpected {url} payload shape")
                rows += more
        total = first.get("total")
        if isinstance(total, int) and len(rows) < total and pages < MAX_PAGES:
            raise TransientError(f"incomplete sweep ({len(rows)} of {total})")
        return rows

    async def _rows(self, only: set[str] | None) -> list:
        terms = {search_term(k) for k in only or ()} - {""}
        if only is not None and len(terms) <= self.search_limit:
            if not terms:
                return []
            found = await asyncio.gather(*(self._sweep(MARKET_URL, "items", search=t)
                                           for t in sorted(terms)))
            rows, slugs = [], set()
            for batch in found:
                for r in batch:
                    if isinstance(r, dict) and r.get("slug") not in slugs:
                        slugs.add(r.get("slug"))
                        rows.append(r)
            return rows
        return await self._sweep(MARKET_URL, "items")

    async def get_listings(self, only: set[str] | None = None) -> list[Listing]:
        rows = await self._rows(only)
        if only is not None:
            rows = [r for r in rows if row_keys(r) & only]
        listings, bad = normalize_products(rows, self.user_id)
        if bad:
            log.debug("bhopp: skipped %d malformed rows", bad)
        if only is not None:
            self._mark_new(only, listings)
        return listings

    def _mark_new(self, only: set[str], listings: list[Listing]):
        """A watched item that was missing on the last poll and is listed now is a new
        release (or back on the market): it counts even on its first sighting."""
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
        rows = await self._sweep(MARKET_URL, "items", search=term)
        listings, _ = normalize_products(rows, self.user_id)
        return [l for l in listings if want in norm_name(l.name) or want == norm_name(l.slug)]

    async def get_listing_details(self, listing: Listing) -> Listing | None:
        data = await self._get(f"{PRODUCTS_URL}/{quote(listing.slug, safe='')}/offers", {})
        offers = data.get("offers")
        if not isinstance(offers, list):
            raise BadResponse("unexpected offers payload shape")
        best = best_offer(offers, self.user_id)
        if best is None:
            return None
        product = {"slug": listing.slug, "name": listing.name, "avatar": listing.image,
                   "rarity": listing.rarity, "category": listing.item_type,
                   "id": listing.extra.get("product_id")}
        return _listing(product, *best)

    def item_names(self) -> list[str]:
        return self._names

    async def refresh_names(self):
        rows = await self._sweep(PRODUCTS_URL, "products")
        names = {str(r.get("name") or "").strip() for r in rows if isinstance(r, dict)}
        if names - {""}:
            self._names = sorted(names - {""})

    # ------------------------------------------------------------ buying

    async def claim_listing(self, listing: Listing, idem_key: str) -> ClaimOutcome:
        """POST /orders/checkout for listing.quantity units of its cheapest offer.

        The site has no idempotency key, so the engine's local reservation is what
        stops a double buy. Errors the site answered with are REJECTED, with its own
        words. Anything sent but not clearly answered is UNKNOWN, and the engine
        reconciles it."""
        offer_id = listing.extra.get("offer_id")
        if not self.token:
            return ClaimOutcome(ClaimStatus.SKIPPED,
                                detail=self.capabilities.claim_unavailable_reason)
        if not offer_id:
            return ClaimOutcome(ClaimStatus.REJECTED, detail="listing has no offer id")
        try:
            await self.read_account()
        except RequestNotSent:
            raise  # engine retries: nothing left the machine
        except (AuthError, BadResponse) as e:
            return ClaimOutcome(ClaimStatus.REJECTED, detail=f"BHOPP session refused: {e}. "
                                "Paste a fresh BHOPP_TOKEN.")
        except MarketplaceError as e:
            return ClaimOutcome(ClaimStatus.SKIPPED, detail=f"BHOPP unreachable, nothing "
                                                            f"bought: {e}")
        qty = max(1, listing.quantity)
        body = {"items": [{"offerId": offer_id, "quantity": qty}]}
        try:
            res = await self._authed("POST", CHECKOUT_URL, json_body=body)
        except RequestNotSent:
            raise
        except AuthError as e:
            return ClaimOutcome(ClaimStatus.REJECTED, detail=f"BHOPP refused the token ({e}). "
                                "Paste a fresh BHOPP_TOKEN.")
        except RateLimited as e:
            return ClaimOutcome(ClaimStatus.REJECTED, detail=f"BHOPP refused: {e}")
        except BadResponse as e:
            if e.status is None or e.status < 400:
                return ClaimOutcome(ClaimStatus.UNKNOWN, detail=f"unreadable answer: {e}")
            return ClaimOutcome(ClaimStatus.REJECTED, detail=f"BHOPP refused: {e}",
                                no_balance=e.code == "insufficient_balance")
        except TransientError as e:
            return ClaimOutcome(ClaimStatus.UNKNOWN, detail=f"no clear answer: {e}")

        orders = res.data.get("orders") if isinstance(res.data, dict) else None
        orders = [o for o in orders or [] if isinstance(o, dict) and o.get("id")]
        if not orders:
            return ClaimOutcome(ClaimStatus.UNKNOWN, detail="checkout answered without an order")
        return self._outcome(orders, listing)

    def _outcome(self, orders: list[dict], listing: Listing) -> ClaimOutcome:
        ids = [str(o["id"]) for o in orders]
        gross = [parse_price((o.get("totals") or {}).get("gross")) for o in orders]
        charged = sum(gross, Decimal(0)) / WALLET if all(g is not None for g in gross) else None
        units = 0
        for o in orders:
            for it in o.get("items") or []:
                if isinstance(it, dict):
                    try:
                        units += int(it.get("quantity") or 0)
                    except (TypeError, ValueError):
                        pass
        seller = str((orders[0].get("seller") or {}).get("username")
                     or listing.extra.get("seller") or "")
        return ClaimOutcome(
            ClaimStatus.CLAIMED, claim_id=",".join(ids), detail=f"order {', '.join(ids)} placed",
            link=chat_url(orders[0].get("conversationId")), username=self.account,
            seller=seller, charged=charged, quantity=units or max(1, listing.quantity),
            note=f"Paid. {seller or 'The seller'} now trades it to your Roblox account; "
                 "confirm receipt on BHOPP once it arrives.")

    async def reconcile_claim(self, listing: Listing) -> tuple[ClaimStatus, str | None]:
        """Look for a recent open order for this listing's offer. Not found is NOT
        proof of no buy (the site may still be creating it), so it stays UNKNOWN."""
        if not self.token:
            return ClaimStatus.UNKNOWN, None
        res = await self._authed("GET", ORDERS_URL,
                                 params={"state": "open", "limit": 10, "page": 1})
        orders = res.data.get("orders") if isinstance(res.data, dict) else None
        want = listing.extra.get("offer_id")
        for o in orders or []:
            if not isinstance(o, dict) or not o.get("id"):
                continue
            if any(isinstance(it, dict) and it.get("offerId") == want
                   for it in o.get("items") or []) and _recent(o.get("createdAt")):
                return ClaimStatus.CLAIMED, str(o["id"])
        return ClaimStatus.UNKNOWN, None


def _recent(stamp) -> bool:
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() <= RECONCILE_WINDOW
