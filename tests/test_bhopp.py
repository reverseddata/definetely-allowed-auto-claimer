"""BHOPP adapter against fake HTTP: payload shapes as observed live (2026-10-25) and
as read from the site's JavaScript. No real requests."""
import base64
import json
import time

import pytest

from conftest import D, FakeHttp, run
from mm2claimer.http import AuthError, BadResponse, RequestNotSent, TransientError
from mm2claimer.marketplaces.bhopp import (CHECKOUT_URL, BhoppAdapter, best_offer,
                                           normalize_products)
from mm2claimer.models import ClaimStatus

ME_ID = "6ad0000000000000000000aa"


def jwt(exp=None, user=ME_ID):
    def part(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{part({'alg': 'HS256'})}.{part({'user': user, 'exp': exp or time.time() + 5 * 86400})}.sig"


def offer(oid, cents, qty=1, seller="s1", name="Seller"):
    return {"id": oid, "amount": cents, "currency": "USD", "quantity": qty,
            "seller": {"id": seller, "username": name}}


def product(slug="harvester", name="Harvester", offers=None, rarity="ancient"):
    return {"id": f"p-{slug}", "slug": slug, "name": name, "avatar": f"https://cdn.test/{slug}.png",
            "rarity": rarity, "category": "knife",
            "offers": offers if offers is not None else [offer("o1", 550, 2), offer("o2", 600, 9)]}


def page(items, pages=1, total=None):
    return {"items": items, "page": 1, "pages": pages,
            "total": len(items) if total is None else total}


CONNECT = {"status": "ok", "authenticated": True,
           "user": {"id": ME_ID, "username": "buyer1", "wallet": {"balance": 123400, "locked": 0}}}


def order(oid="ord-1", offer_id="o1", qty=2, unit=550, gross=None, conv="conv-9"):
    return {"id": oid, "state": "pending", "seller": {"id": "s1", "username": "Seller"},
            "items": [{"offerId": offer_id, "productId": "p-harvester", "name": "Harvester",
                       "quantity": qty, "unitAmount": unit}],
            "totals": {"gross": gross if gross is not None else unit * qty * 100,
                       "fee": 0, "net": 0},
            "conversationId": conv, "createdAt": "2099-01-01T00:00:00.000Z"}


def adapter(http, token=None, **kw):
    return BhoppAdapter(http, token=jwt() if token is None else token, **kw)


def buy_listing(qty=2):
    l = normalize_products([product()])[0][0]
    from dataclasses import replace
    return replace(l, quantity=qty)


# ---------------------------------------------------------------- monitoring


def test_cents_cheapest_offer_and_quantity():
    ls, bad = normalize_products([product()])
    assert bad == 0 and len(ls) == 1
    l = ls[0]
    assert (l.listing_id, l.price, l.quantity, l.unit) == ("harvester", D("5.5"), 2, "USD")
    assert l.extra["offer_id"] == "o1" and l.extra["seller"] == "Seller"
    assert l.url == "https://bhopp.com/products/harvester"


def test_own_offers_and_junk_are_never_the_cheapest():
    offers = [offer("mine", 100, 5, seller=ME_ID), offer("eur", 50, 1), offer("zero", 0, 1),
              offer("noqty", 90, 0), offer("ok", 700, 1)]
    offers[1]["currency"] = "EUR"
    o, price = best_offer(offers, own_id=ME_ID)
    assert o["id"] == "ok" and price == D("7")
    # Only our own offer: nothing to buy, so the product is not listed for us.
    assert normalize_products([product(offers=[offer("mine", 100, 5, seller=ME_ID)])],
                              own_id=ME_ID)[0] == []


def test_chroma_marker_is_not_a_rarity():
    l = normalize_products([product("chroma-seer", "Chroma Seer", rarity="chroma")])[0][0]
    assert l.chroma and l.rarity is None
    l = normalize_products([product("purple-seer", "Purple Seer", rarity="chroma")])[0][0]
    assert not l.chroma   # BHOPP mis-tags some plain godlies; the name decides


def test_malformed_rows_skipped_bad_shape_raises():
    ls, bad = normalize_products([{"name": "x"}, "junk", product()])
    assert len(ls) == 1 and bad == 2
    with pytest.raises(BadResponse):
        normalize_products({"items": []})


def test_few_watched_names_are_searched_not_swept():
    http = FakeHttp(("GET", "/market", page([product(), product("seer", "Seer")])))
    ls = run(adapter(http).get_listings(only={"harvester"}))
    assert [l.name for l in ls] == ["Harvester"]       # only watched rows are parsed
    assert http.calls[0]["params"]["search"] == "harvester"


def test_many_watched_names_sweep_every_page():
    http = FakeHttp(("GET", "/market", page([product()], pages=2, total=2)),
                    ("GET", "/market", page([product("seer", "Seer")], pages=2, total=2)))
    ls = run(adapter(http, search_limit=1).get_listings(only={"harvester", "seer"}))
    assert {l.name for l in ls} == {"Harvester", "Seer"}
    assert [c["params"].get("page") for c in http.calls] == [1, 2]
    assert all("search" not in c["params"] for c in http.calls)


def test_incomplete_sweep_raises_instead_of_reporting_sold_out():
    http = FakeHttp(("GET", "/market", page([product()], pages=1, total=5)))
    with pytest.raises(TransientError):
        run(adapter(http, search_limit=0).get_listings(only={"harvester", "seer"}))


def test_item_listed_after_being_missing_is_a_new_release():
    a = adapter(FakeHttp(("GET", "/market", page([])),
                         ("GET", "/market", page([product()]))))
    assert run(a.get_listings(only={"harvester"})) == []
    l = run(a.get_listings(only={"harvester"}))[0]
    assert l.extra.get("new_release") is True


def test_item_listed_on_first_poll_is_not_a_new_release():
    a = adapter(FakeHttp(("GET", "/market", page([product()]))))
    assert "new_release" not in run(a.get_listings(only={"harvester"}))[0].extra


def test_revalidation_reads_the_product_offers():
    http = FakeHttp(("GET", "/products/harvester/offers",
                     {"offers": [offer("o9", 499, 3), offer("o1", 550, 2)]}))
    fresh = run(adapter(http).get_listing_details(buy_listing()))
    assert (fresh.price, fresh.quantity, fresh.extra["offer_id"]) == (D("4.99"), 3, "o9")
    http = FakeHttp(("GET", "/offers", {"offers": []}))
    assert run(adapter(http).get_listing_details(buy_listing())) is None


# ---------------------------------------------------------------- session


def test_capabilities_follow_token():
    assert adapter(FakeHttp(), token="").capabilities.claim is False
    assert adapter(FakeHttp()).capabilities.claim is True


def test_balance_is_in_ten_thousandths():
    http = FakeHttp(("GET", "/connect", CONNECT))
    a = adapter(http)
    assert run(a.get_balance()) == D("12.34")
    assert a.account == "buyer1"
    assert http.calls[0]["headers"]["Authorization"].startswith("Bearer ")


def test_logged_out_token_is_an_auth_error():
    http = FakeHttp(("GET", "/connect", {"status": "ok", "authenticated": False, "user": None}))
    with pytest.raises(AuthError):
        run(adapter(http).read_account())


def test_expiry_warning():
    assert adapter(FakeHttp()).session_warning() is None
    soon = adapter(FakeHttp(), token=jwt(exp=time.time() + 3600))
    assert "expires" in soon.session_warning()
    gone = adapter(FakeHttp(), token=jwt(exp=time.time() - 10))
    assert "expired" in gone.session_warning()


# ---------------------------------------------------------------- buying


def test_checkout_body_and_outcome():
    http = FakeHttp(("GET", "/connect", CONNECT),
                    ("POST", "/orders/checkout", {"orders": [order(gross=110000)]}))
    out = run(adapter(http).claim_listing(buy_listing(), "k"))
    assert out.status is ClaimStatus.CLAIMED and out.claim_id == "ord-1"
    post = http.posts()[0]
    assert post["url"] == CHECKOUT_URL
    assert post["json"] == {"items": [{"offerId": "o1", "quantity": 2}]}
    assert out.charged == D("11") and out.quantity == 2
    assert out.link == "https://bhopp.com/account/messages/conv-9"
    assert out.username == "buyer1" and out.seller == "Seller"


def test_insufficient_balance_is_a_refusal_with_the_site_words():
    http = FakeHttp(("GET", "/connect", CONNECT),
                    ("POST", "/orders/checkout",
                     BadResponse("HTTP 400: Not enough balance.", 400, "insufficient_balance")))
    out = run(adapter(http).claim_listing(buy_listing(), "k"))
    assert out.status is ClaimStatus.REJECTED and out.no_balance
    assert "Not enough balance" in out.detail


def test_other_refusals_keep_the_site_text():
    http = FakeHttp(("GET", "/connect", CONNECT),
                    ("POST", "/orders/checkout", BadResponse("HTTP 409: Offer sold out.", 409)))
    out = run(adapter(http).claim_listing(buy_listing(), "k"))
    assert out.status is ClaimStatus.REJECTED and not out.no_balance
    assert "Offer sold out" in out.detail


@pytest.mark.parametrize("exc", [TransientError("timeout"), TransientError("HTTP 502")])
def test_no_clear_answer_is_unknown(exc):
    http = FakeHttp(("GET", "/connect", CONNECT), ("POST", "/orders/checkout", exc))
    assert run(adapter(http).claim_listing(buy_listing(), "k")).status is ClaimStatus.UNKNOWN


def test_answer_without_orders_is_unknown():
    http = FakeHttp(("GET", "/connect", CONNECT), ("POST", "/orders/checkout", {"orders": []}))
    assert run(adapter(http).claim_listing(buy_listing(), "k")).status is ClaimStatus.UNKNOWN


def test_dead_token_is_rejected_before_any_purchase():
    http = FakeHttp(("GET", "/connect", {"authenticated": False}))
    out = run(adapter(http).claim_listing(buy_listing(), "k"))
    assert out.status is ClaimStatus.REJECTED and "BHOPP_TOKEN" in out.detail
    assert http.posts() == []


def test_request_not_sent_propagates_for_a_safe_retry():
    http = FakeHttp(("GET", "/connect", CONNECT),
                    ("POST", "/orders/checkout", RequestNotSent("connect failed")))
    with pytest.raises(RequestNotSent):
        run(adapter(http).claim_listing(buy_listing(), "k"))


def test_no_token_never_sends():
    http = FakeHttp()
    out = run(adapter(http, token="").claim_listing(buy_listing(), "k"))
    assert out.status is ClaimStatus.SKIPPED and http.calls == []


def test_reconcile_finds_the_recent_order_for_our_offer():
    http = FakeHttp(("GET", "/orders", {"orders": [order("other", offer_id="zz"), order()],
                                        "counts": {}}))
    assert run(adapter(http).reconcile_claim(buy_listing())) == (ClaimStatus.CLAIMED, "ord-1")
    old = order()
    old["createdAt"] = "2020-01-01T00:00:00Z"
    http = FakeHttp(("GET", "/orders", {"orders": [old]}))
    assert run(adapter(http).reconcile_claim(buy_listing())) == (ClaimStatus.UNKNOWN, None)


def test_names_come_from_the_catalogue():
    http = FakeHttp(("GET", "/products", {"products": [{"name": "Seer"}, {"name": "Harvester"}],
                                          "pages": 1, "total": 2}))
    a = adapter(http)
    run(a.refresh_names())
    assert a.item_names() == ["Harvester", "Seer"]


def test_token_is_redacted():
    from mm2claimer.notify import redact
    tok = jwt()
    adapter(FakeHttp(), token=tok)
    assert tok not in redact(f"header Authorization: Bearer {tok} and raw {tok}")
