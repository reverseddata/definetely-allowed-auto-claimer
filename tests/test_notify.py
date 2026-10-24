from conftest import D, listing
from mm2claimer.models import ClaimOutcome, ClaimStatus
from mm2claimer.notify import (available_embed, claim_embed, claim_failed_embed, redact,
                               register_secret)


def test_claim_embed_bhopp():
    l = listing("bhopp", price="5.5", qty=2)
    e = claim_embed(l, ClaimOutcome(ClaimStatus.CLAIMED, "ord-1", link="https://bhopp.com/account/messages/c",
                                    seller="Seller_1", username="buyer", charged=D("11"),
                                    quantity=2, note="Paid. Seller_1 now trades it."))
    assert e["title"] == "✅ 2× Harvester bought"
    assert "BHOPP" in e["description"] and "account/messages/c" in e["description"]
    fields = {f["name"]: f["value"] for f in e["fields"]}
    assert fields["Total"] == "$11" and fields["Seller"] == "Seller\\_1"


def test_links_must_be_https():
    l = listing(price="1")
    l = l.__class__(**{**l.__dict__, "url": "javascript:alert(1)"})
    e = claim_embed(l, ClaimOutcome(ClaimStatus.CLAIMED, link="roblox://x"))
    assert e["url"] is None and "roblox://" not in e["description"]
    assert available_embed(l, "monitor")["url"] is None


def test_unknown_outcome_says_buying_is_paused():
    e = claim_failed_embed(listing("bloxymarket"), ClaimOutcome(ClaimStatus.UNKNOWN,
                                                                detail="timeout"))
    assert "outcome unknown" in e["title"] and "BloxyMarket" in e["description"]


def test_redaction():
    register_secret("supersecretcookievalue")
    text = redact("Cookie: a=supersecretcookievalue; Authorization: Bearer abc.def "
                  "eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyIjoiMTIzNDU2Nzg5MCJ9.c2lnbmF0dXJlLXZhbHVl "
                  "https://discord.com/api/webhooks/1/abc")
    assert "supersecretcookievalue" not in text and "eyJ" not in text
    assert "webhooks/1/abc" not in text and "abc.def" not in text
