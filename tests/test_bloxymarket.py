"""BloxyMarket adapter against fake HTTP: payload shapes as observed live (2026-10-25)
and as read from the site's JavaScript. No real requests."""
import json
from dataclasses import replace

import pytest

from conftest import D, FakeHttp, run
from mm2claimer.http import AuthError, BadResponse, HttpResult, RateLimited, TransientError
from mm2claimer.marketplaces.bloxymarket import (PURCHASE_URL, BloxyMarketAdapter,
                                                 normalize_rows)
from mm2claimer.models import ClaimStatus

COOKIE = "session=abc123secret; other=1"


def row(name="Harvester", units=(("u1", 5.75), ("u2", 5.75), ("u3", 6.0)), chroma=False,
        game=None):
    return {"game_name": game or name, "display_name": name, "thumbnail": "https://img.test/x",
            "item_type": "Knife", "rarity": "Ancient", "chroma": chroma,
            "listings": [{"_id": i, "price": p} for i, p in units]}


def market(rows, total=None):
    return {"message": "success", "page": 0, "page_size": 50,
            "total_items": len(rows) if total is None else total, "results": rows}


AUTH = {"message": "success", "username": "buyer1", "email": "x@example.test"}


def adapter(http, cookie=COOKIE, **kw):
    return BloxyMarketAdapter(http, cookie=cookie, **kw)


def buy_listing(qty=2):
    return replace(normalize_rows([row()])[0][0], quantity=qty)


# ---------------------------------------------------------------- monitoring


def test_cheapest_price_and_its_units():
    l = normalize_rows([row()])[0][0]
    assert (l.listing_id, l.price, l.quantity) == ("Harvester", D("5.75"), 2)
    assert l.extra["ids"] == ["u1", "u2"]
    assert l.url == "https://bloxymarket.com/market/item/u1"


def test_unknown_rarity_and_junk_units():
    r = row(units=(("a", 0), ("b", None), ("", 1), ("c", 2.5)))
    r["rarity"] = "Unknown"
    l = normalize_rows([r, "junk", {"listings": []}])[0][0]
    assert (l.price, l.extra["ids"], l.rarity) == (D("2.5"), ["c"], None)


def test_search_uses_display_name_filter():
    http = FakeHttp(("GET", "/v1/marketplace", market([row(), row("Seer")])))
    ls = run(adapter(http).get_listings(only={"harvester"}))
    assert [l.name for l in ls] == ["Harvester"]
    p = http.calls[0]["params"]
    assert (p["display_name"], p["sort"], p["game_type"], p["page"]) == \
        ("harvester", "low_to_high", "mm2", 0)


def test_full_sweep_pages_until_total():
    full = [row(f"Item{i}") for i in range(50)]
    http = FakeHttp(("GET", "/v1/marketplace", market(full, total=51)),
                    ("GET", "/v1/marketplace", market([row("Last")], total=51)))
    ls = run(adapter(http, search_limit=0).get_listings(only={"last"}))
    assert [l.name for l in ls] == ["Last"]
    assert [c["params"]["page"] for c in http.calls] == [0, 1]


def test_rate_limit_propagates():
    http = FakeHttp(("GET", "/v1/marketplace", RateLimited(60)))
    with pytest.raises(RateLimited):
        run(adapter(http).get_listings(only={"harvester"}))


def test_unsuccessful_market_answer_is_bad_response():
    http = FakeHttp(("GET", "/v1/marketplace", {"message": "error"}))
    with pytest.raises(BadResponse):
        run(adapter(http).get_listings(only={"harvester"}))


# ---------------------------------------------------------------- buying


def test_purchase_body_locks_each_unit_price():
    http = FakeHttp(("GET", "/authenticate", AUTH), ("POST", "/v1/purchase",
                                                     {"message": "success"}))
    out = run(adapter(http).claim_listing(buy_listing(), "k"))
    assert out.status is ClaimStatus.CLAIMED
    post = http.posts()[0]
    assert post["url"] == PURCHASE_URL
    assert post["json"] == {"items": [{"item_id": "u1", "expected_price": 5.75},
                                      {"item_id": "u2", "expected_price": 5.75}]}
    assert post["headers"]["Cookie"] == COOKIE
    assert post["spaced"] is False      # never waits behind the poll's request gap
    assert (out.quantity, out.charged, out.username) == (2, D("11.50"), "buyer1")
    assert "inventory" in out.note


def test_refusal_in_message_keeps_site_words():
    http = FakeHttp(("GET", "/authenticate", AUTH),
                    ("POST", "/v1/purchase", {"message": "Item is no longer available"}))
    out = run(adapter(http).claim_listing(buy_listing(), "k"))
    assert out.status is ClaimStatus.REJECTED and "no longer available" in out.detail
    assert not out.no_balance


@pytest.mark.parametrize("answer", [{"message": "Insufficient balance"},
                                    BadResponse("HTTP 400: Not enough funds", 400)])
def test_no_money_refusals_are_flagged(answer):
    http = FakeHttp(("GET", "/authenticate", AUTH), ("POST", "/v1/purchase", answer))
    out = run(adapter(http).claim_listing(buy_listing(), "k"))
    assert out.status is ClaimStatus.REJECTED and out.no_balance


@pytest.mark.parametrize("answer", [TransientError("timeout"), {"nothing": 1}, ["x"]])
def test_unclear_answers_are_unknown(answer):
    http = FakeHttp(("GET", "/authenticate", AUTH),
                    ("POST", "/v1/purchase", answer if isinstance(answer, Exception)
                     else HttpResult(200, answer)))
    assert run(adapter(http).claim_listing(buy_listing(), "k")).status is ClaimStatus.UNKNOWN


def test_logged_out_session_is_rejected_before_purchase():
    http = FakeHttp(("GET", "/authenticate", {"message": "Not authenticated"}))
    out = run(adapter(http).claim_listing(buy_listing(), "k"))
    assert out.status is ClaimStatus.REJECTED and "BLOXYMARKET_COOKIE" in out.detail
    assert http.posts() == []


def test_401_on_purchase_is_rejected():
    http = FakeHttp(("GET", "/authenticate", AUTH), ("POST", "/v1/purchase", AuthError("401")))
    assert run(adapter(http).claim_listing(buy_listing(), "k")).status is ClaimStatus.REJECTED


def test_balance_and_account():
    http = FakeHttp(("GET", "/v1/balance", {"message": "success",
                                            "balance": {"total": 42.5, "withdrawable": 10}}),
                    ("GET", "/authenticate", {"message": "Unauthorized"}))
    a = adapter(http)
    assert run(a.get_balance()) == D("42.5")
    with pytest.raises(AuthError):
        run(a.read_account())


def test_reconcile_finds_bought_units_in_inventory():
    inv = {"message": "success", "items": [{"display_name": "Harvester", "listed_ids": [],
                                            "unlisted_ids": ["zz", "u2"]}]}
    http = FakeHttp(("GET", "/v2/inventory", inv))
    assert run(adapter(http).reconcile_claim(buy_listing())) == (ClaimStatus.CLAIMED, "u2")
    assert http.calls[0]["params"]["display_name"] == "Harvester"
    http = FakeHttp(("GET", "/v2/inventory", {"message": "success", "items": []}))
    assert run(adapter(http).reconcile_claim(buy_listing()))[0] is ClaimStatus.UNKNOWN


# ---------------------------------------------------------------- session


def test_rotated_cookies_are_kept_saved_and_reloaded(tmp_path):
    path = tmp_path / "bloxy.json"
    http = FakeHttp(("GET", "/v1/balance", HttpResult(
        200, {"message": "success", "balance": {"total": 1}}, cookies={"session": "rotated99"})),
        ("GET", "/v1/balance", {"message": "success", "balance": {"total": 1}}))
    a = adapter(http, session_path=path)
    run(a.get_balance())
    run(a.get_balance())
    assert http.calls[1]["headers"]["Cookie"] == "session=rotated99; other=1"
    saved = json.loads(path.read_text())
    assert saved["cookies"]["session"] == "rotated99" and "token_sha" in saved
    # Same paste in .env: the saved rotation is used. A new paste wins.
    assert adapter(FakeHttp(), session_path=path).jar["session"] == "rotated99"
    assert adapter(FakeHttp(), cookie="session=new", session_path=path).jar["session"] == "new"


def test_cookie_values_are_redacted():
    from mm2claimer.notify import redact
    adapter(FakeHttp())
    assert "abc123secret" not in redact("sent session=abc123secret upstream")
