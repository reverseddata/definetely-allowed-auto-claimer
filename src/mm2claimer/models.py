"""Typed records shared by the adapters, the watcher and the claim engine."""
from __future__ import annotations

import enum
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?|-?\.\d+")


def parse_price(raw) -> Decimal | None:
    """Parse a marketplace price into a non-negative Decimal, or None.

    Accepts numbers and strings like "1.2", "1,200", " 7 ". Missing, empty,
    non-numeric, negative, NaN or infinite input gives None. A malformed price is
    "unknown", never zero, so it can't look like a free item.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, float) and (math.isnan(raw) or math.isinf(raw)):
        return None
    if isinstance(raw, (int, float, Decimal)):
        val = Decimal(str(raw))
    elif isinstance(raw, str):
        m = _NUMBER.search(raw.strip())
        if not m:
            return None
        try:
            val = Decimal(m.group().replace(",", ""))
        except InvalidOperation:
            return None
    else:
        return None
    if not val.is_finite() or val < 0:
        return None
    return val


def norm_name(s: str) -> str:
    """Matching key: lowercase, alphanumerics, single spaces, possessives dropped,
    and the "C." chroma shorthand spelled out.
    "C. Seer" / "chroma-seer" -> "chroma seer"; "Traveler's Axe" -> "traveler axe"."""
    t = re.sub(r"'s\b", "", (s or "").lower())
    t = re.sub(r"[^a-z0-9]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if t.startswith("c "):
        t = "chroma " + t[2:]
    return t


def search_term(key: str) -> str:
    """What to type into a site's search box to find the item with this watch key.
    Both sites match a substring of the display name, so one word is enough. The
    longest word that is not "chroma" is the most selective, and survives
    punctuation the key dropped ("traveler axe" -> "traveler" finds "Traveler's Axe")."""
    words = [w for w in key.split() if w != "chroma"] or key.split()
    return max(words, key=len) if words else ""


@dataclass(frozen=True)
class Listing:
    """The cheapest units of one item on one marketplace, right now."""
    marketplace: str
    listing_id: str            # stable per item (BHOPP: product slug)
    slug: str                  # the item's own id on the marketplace
    name: str                  # display name, chromas as "Chroma <name>"
    price: Decimal             # cheapest unit price, in `unit`
    unit: str                  # e.g. "USD"
    available: bool            # anything to buy at all
    quantity: int = 0          # units at that cheapest price
    side: str = "withdraw"     # always "buy"; kept so the ledger reads like BloxSwaps'
    rarity: str | None = None
    chroma: bool = False
    item_type: str | None = None
    url: str = ""
    image: str = ""
    detected_at: datetime = field(default_factory=utcnow)
    # Adapter data the claim needs (offer id, unit ids). JSON-safe values only:
    # it is saved with the claim, to find the purchase again after an unclear answer.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Capabilities:
    monitoring: bool = True
    claim: bool = False
    claim_unavailable_reason: str = ""


@dataclass
class MarketplaceStatus:
    name: str
    capabilities: Capabilities
    reachable: bool = False
    last_success: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    listings_tracked: int = 0
    polls: int = 0
    rate_limited_until: datetime | None = None
    paused_reason: str | None = None

    def as_dict(self) -> dict:
        iso = lambda d: d.isoformat() if d else None  # noqa: E731
        return {
            "name": self.name,
            "reachable": self.reachable,
            "last_success": iso(self.last_success),
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "listings_tracked": self.listings_tracked,
            "polls": self.polls,
            "rate_limited_until": iso(self.rate_limited_until),
            "paused_reason": self.paused_reason,
            "supports_claim": self.capabilities.claim,
            "claim_unavailable_reason": self.capabilities.claim_unavailable_reason,
        }


class ClaimStatus(str, enum.Enum):
    PENDING = "pending"          # reserved locally, request not answered yet
    CLAIMED = "claimed"          # bought: the site took the money
    COMPLETED = "completed"      # operator marked an unclear buy as done (`resume`)
    EXPIRED = "expired"          # the site shows the buy was cancelled
    REJECTED = "rejected"        # site refused (sold, price moved, balance...)
    TIMED_OUT = "timed_out"      # unclear buy never showed up on the site in time
    UNKNOWN = "unknown"          # outcome unclear: reconcile, never retry blindly
    SKIPPED = "skipped"          # failed revalidation or limits before any request


# A claim in one of these blocks further claims on its marketplace.
OPEN_STATUSES = (ClaimStatus.PENDING, ClaimStatus.UNKNOWN)


@dataclass(frozen=True)
class ClaimOutcome:
    status: ClaimStatus
    claim_id: str | None = None
    detail: str = ""
    # True only when the request provably never reached the marketplace.
    safe_to_retry: bool = False
    link: str = ""               # where to follow the purchase (order chat, inventory)
    username: str = ""           # the account that bought it
    seller: str = ""             # who delivers it (BHOPP)
    note: str = ""               # what happens next, in the site's terms
    # What the site charged for the whole purchase, in the listing's unit.
    charged: Decimal | None = None
    quantity: int = 0            # units the site says it sold us
    # The site refused because the balance is too low: stop until it is topped up.
    no_balance: bool = False
