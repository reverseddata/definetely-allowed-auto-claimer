"""Adapter interface. An adapter knows one marketplace's URLs, payload shapes
and capabilities, and nothing about watchlists, Discord or limits."""
from __future__ import annotations

import abc
from decimal import Decimal

from ..models import Capabilities, ClaimOutcome, ClaimStatus, Listing, MarketplaceStatus


class MarketplaceAdapter(abc.ABC):
    name: str = ""
    unit: str = "USD"
    capabilities: Capabilities = Capabilities()
    # Account name the adapter read from its own session ("" if not known yet).
    # Shown in buy alerts, so the user never has to configure it.
    account: str = ""
    # get_listings(only=...) parses (and returns) only the watched items.
    supports_only: bool = True
    # The buy request itself makes the site refuse a changed price or a sold unit,
    # so the engine skips its own re-check (which would cost a request).
    server_checks_price: bool = False

    def __init__(self):
        self.status = MarketplaceStatus(self.name, self.capabilities)

    async def connect(self) -> None:
        """Prepare sessions / verify credentials. Must not raise for a missing
        optional credential; reflect it in capabilities instead."""

    async def close(self) -> None:
        """Release network resources."""

    @abc.abstractmethod
    async def get_listings(self, only: set[str] | None = None) -> list[Listing]:
        """One complete poll: every listed item, or with `only`, the items whose
        name or slug is one of those watch keys. Raises http.MarketplaceError on any
        failure, never returns a partial result (missing items read as sold out).
        Malformed rows are skipped, never fatal."""

    async def lookup(self, item: str) -> list[Listing]:
        """Listings whose name contains `item` (the /watch add check)."""
        from ..models import norm_name
        want = norm_name(item)
        return [l for l in await self.get_listings() if want in norm_name(l.name)]

    async def get_listing_details(self, listing: Listing) -> Listing | None:
        """Fresh state of one item (re-checked right before a buy).
        None if nothing is listed any more."""
        for l in await self.get_listings(only={listing.listing_id}):
            if l.listing_id == listing.listing_id:
                return l
        return None

    def get_marketplace_status(self) -> MarketplaceStatus:
        return self.status

    def item_names(self) -> list[str]:
        """Known item names, for Discord autocomplete."""
        return []

    async def refresh_names(self) -> None:
        """Reload the names behind item_names() (called now and then)."""

    async def get_balance(self) -> Decimal:
        raise NotImplementedError

    async def read_account(self) -> str:
        raise NotImplementedError

    def session_warning(self) -> str | None:
        """Something the user must do about the session soon (it expires), or None."""
        return None

    async def claim_listing(self, listing: Listing, idem_key: str) -> ClaimOutcome:
        """Buy listing.quantity units at listing.price. Must return UNKNOWN (not
        raise, not REJECTED) when the request may have reached the site but no clear
        answer came back."""
        return ClaimOutcome(ClaimStatus.SKIPPED,
                            detail=self.capabilities.claim_unavailable_reason
                            or "buying is not supported")

    async def reconcile_claim(self, listing: Listing) -> tuple[ClaimStatus, str | None]:
        """After an unclear answer: did our buy go through? CLAIMED plus the site's
        id if it shows the purchase. Anything the site does not clearly show must be
        UNKNOWN, never REJECTED: the request may still be processing."""
        return ClaimStatus.UNKNOWN, None
