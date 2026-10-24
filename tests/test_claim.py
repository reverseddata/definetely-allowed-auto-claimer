"""Claim engine: sizing, limits, balance, unclear answers, per-market holds."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from conftest import D, FakeAdapter, FakeNotifier, listing, make_settings, run
from mm2claimer.claim import ClaimEngine
from mm2claimer.config import WatchEntry
from mm2claimer.http import RequestNotSent, TransientError
from mm2claimer.marketplaces.multi import MultiMarketAdapter
from mm2claimer.models import ClaimOutcome, ClaimStatus
from mm2claimer.store import Hold, Store
from mm2claimer.watchlist import Limits

W = WatchEntry("Harvester", "both", D("6"), "harvester")


def setup(tmp_path, adapter, **env):
    s = make_settings(tmp_path, "claim", extra_env=env)
    store = Store(":memory:")
    n = FakeNotifier()
    return s, store, n, ClaimEngine(s, store, adapter, n)


def later(l, seconds=1):
    return replace(l, detected_at=datetime.now(timezone.utc) + timedelta(seconds=seconds))


def test_buy_records_and_pings_and_does_not_pause(tmp_path):
    l = listing(qty=2)
    a = FakeAdapter("bhopp", [l])
    s, store, n, eng = setup(tmp_path, a)
    out = run(eng.attempt(l, W))
    assert out.status is ClaimStatus.CLAIMED and n.kinds() == ["claimed"]
    assert store.holds() == []
    row = store.recent_claims()[0]
    assert (row["status"], row["quantity"], D(row["price"])) == ("claimed", 2, D("11.0"))
    # No pause after a buy: the next sighting buys again.
    assert run(eng.attempt(later(l), W)).status is ClaimStatus.CLAIMED
    assert len(a.calls) == 2


def test_takes_all_units_the_balance_affords(tmp_path):
    l = listing(price="2", qty=10)
    a = FakeAdapter("bhopp", [l], balance=D("7.5"))
    s, store, n, eng = setup(tmp_path, a)
    run(eng.attempt(l, W))
    assert a.calls[0][0].quantity == 3


def test_caps_on_units(tmp_path):
    l = listing(price="1", qty=10)
    a = FakeAdapter("bhopp", [l])
    s, store, n, eng = setup(tmp_path, a, MAX_CLAIM_QUANTITY="4")
    run(eng.attempt(l, replace(W, max_qty=6)))
    assert a.calls[0][0].quantity == 4
    run(eng.attempt(later(l), replace(W, max_qty=2)))
    assert a.calls[1][0].quantity == 2


def test_no_balance_skips_without_a_request_and_alerts(tmp_path):
    l = listing(price="5.5")
    a = FakeAdapter("bhopp", [l], balance=D("3"))
    s, store, n, eng = setup(tmp_path, a)
    out = run(eng.attempt(l, W))
    assert out.status is ClaimStatus.SKIPPED and "balance" in out.detail
    assert a.calls == [] and store.recent_claims() == []
    assert n.titles() == ["💸 Out of balance on BHOPP"]


def test_site_no_balance_refusal_stops_further_tries(tmp_path):
    l = listing(price="1", qty=1)
    a = FakeAdapter("bhopp", [l], outcomes=[ClaimOutcome(
        ClaimStatus.REJECTED, detail="BHOPP refused: HTTP 400: Not enough balance",
        no_balance=True)])
    s, store, n, eng = setup(tmp_path, a)
    run(eng.attempt(l, W))
    assert "Out of balance" in n.titles()[0] and "Not enough balance" in n.events[0][2]
    out = run(eng.attempt(later(l), W))     # cached balance is now 0: no request
    assert out.status is ClaimStatus.SKIPPED and len(a.calls) == 1


def test_balance_read_failure_still_buys(tmp_path):
    l = listing(qty=1)
    a = FakeAdapter("bhopp", [l], balance=TransientError("timeout"))
    s, store, n, eng = setup(tmp_path, a)
    assert run(eng.attempt(l, W)).status is ClaimStatus.CLAIMED


def test_balance_is_cached_and_spent_down(tmp_path):
    l = listing(price="2", qty=1)
    a = FakeAdapter("bhopp", [l], balance=D("5"))
    s, store, n, eng = setup(tmp_path, a)
    run(eng.attempt(l, W))
    run(eng.attempt(later(l), W))
    out = run(eng.attempt(later(l, 2), W))   # 5 - 2 - 2 = 1 left, below 2
    assert a.balance_reads == 1 and len(a.calls) == 2
    assert out.status is ClaimStatus.SKIPPED


def test_daily_cap_grants_fewer_then_blocks_and_alerts_once(tmp_path):
    l = listing(price="2", qty=5)
    a = FakeAdapter("bhopp", [l])
    s, store, n, eng = setup(tmp_path, a, MAX_DAILY_SPEND="5")
    run(eng.attempt(l, W))
    assert a.calls[0][0].quantity == 2       # 2 x $2 fits in $5
    for i in range(2):
        out = run(eng.attempt(later(l, i + 1), W))
        assert out.status is ClaimStatus.SKIPPED and "daily cap" in out.detail
    assert n.titles().count("Daily spend cap reached") == 1


def test_discord_limits_override_env(tmp_path):
    l = listing(price="5.5", qty=1)
    a = FakeAdapter("bhopp", [l])
    s, store, n, eng = setup(tmp_path, a, MAX_CLAIM_PRICE="10")
    Limits(s, store).set("max_claim_price", "5")
    assert run(eng.attempt(l, W)).status is ClaimStatus.SKIPPED
    Limits(s, store).set("max_claim_price", None)   # back to .env
    assert run(eng.attempt(later(l), W)).status is ClaimStatus.CLAIMED


def test_watch_price_and_min_qty(tmp_path):
    a = FakeAdapter("bhopp")
    s, store, n, eng = setup(tmp_path, a)
    assert run(eng.attempt(listing(price="6.01"), W)).status is ClaimStatus.SKIPPED
    assert run(eng.attempt(listing(qty=2), replace(W, min_qty=3))).status is ClaimStatus.SKIPPED
    assert a.calls == []


def test_revalidation_price_rise_or_sold_out_skips(tmp_path):
    l = listing(price="5", qty=1)
    a = FakeAdapter("bhopp", details=lambda x: replace(x, price=D("5.9")))
    s, store, n, eng = setup(tmp_path, a)
    out = run(eng.attempt(l, W))
    assert out.status is ClaimStatus.SKIPPED and "rose" in out.detail and a.calls == []
    a.details = lambda x: None
    assert "sold out" in run(eng.attempt(later(l), W)).detail


def test_no_separate_recheck_where_the_buy_checks_the_price(tmp_path):
    l = listing("bloxymarket", price="5", qty=1)
    a = FakeAdapter("bloxymarket", details=lambda x: (_ for _ in ()).throw(AssertionError))
    a.server_checks_price = True
    s, store, n, eng = setup(tmp_path, a)
    assert run(eng.attempt(l, W)).status is ClaimStatus.CLAIMED


def test_cheaper_on_recheck_buys_at_the_fresh_price(tmp_path):
    l = listing(price="5", qty=1)
    a = FakeAdapter("bhopp", details=lambda x: replace(x, price=D("4"), quantity=3,
                                                       extra={"offer_id": "new"}))
    s, store, n, eng = setup(tmp_path, a)
    run(eng.attempt(l, W))
    sent = a.calls[0][0]
    assert (sent.price, sent.quantity, sent.extra["offer_id"]) == (D("4"), 1, "new")


def test_concurrent_attempts_buy_once_per_sighting(tmp_path):
    l = listing(qty=1)
    a = FakeAdapter("bhopp", [l], claim_delay=0.05)
    s, store, n, eng = setup(tmp_path, a)

    async def go():
        return await asyncio.gather(*(eng.attempt(l, W) for _ in range(10)))
    outs = run(go())
    assert len(a.calls) == 1
    assert sum(o.status is ClaimStatus.CLAIMED for o in outs) == 1


def test_request_not_sent_is_retried(tmp_path):
    l = listing(qty=1)
    a = FakeAdapter("bhopp", [l], claim_exc=[RequestNotSent("dns")])
    s, store, n, eng = setup(tmp_path, a)
    assert run(eng.attempt(l, W)).status is ClaimStatus.CLAIMED and len(a.calls) == 2


def two_markets(tmp_path, bhopp_outcomes=None, reconcile=None):
    bh = FakeAdapter("bhopp", outcomes=bhopp_outcomes, reconcile=reconcile)
    bx = FakeAdapter("bloxymarket")
    multi = MultiMarketAdapter({"bhopp": bh, "bloxymarket": bx},
                               lambda: {"bhopp", "bloxymarket"})
    return bh, bx, multi, *setup(tmp_path, multi)


def test_unclear_answer_holds_only_that_market(tmp_path):
    bh, bx, multi, s, store, n, eng = two_markets(
        tmp_path, [ClaimOutcome(ClaimStatus.UNKNOWN, detail="timeout")])
    out = run(eng.attempt(listing("bhopp"), W))
    assert out.status is ClaimStatus.UNKNOWN
    assert [h.market for h in store.holds()] == ["bhopp"]
    assert n.kinds() == ["failed"]
    # BHOPP is held, BloxyMarket keeps buying.
    assert run(eng.attempt(later(listing("bhopp")), W)).status is ClaimStatus.SKIPPED
    assert run(eng.attempt(listing("bloxymarket"), W)).status is ClaimStatus.CLAIMED
    assert len(bh.calls) == 1 and len(bx.calls) == 1


def test_unclear_answer_confirmed_by_reconcile_is_a_buy(tmp_path):
    bh, bx, multi, s, store, n, eng = two_markets(
        tmp_path, [ClaimOutcome(ClaimStatus.UNKNOWN, detail="timeout")],
        reconcile=[(ClaimStatus.CLAIMED, "ord-7")])
    out = run(eng.attempt(listing("bhopp"), W))
    assert out.status is ClaimStatus.CLAIMED and out.claim_id == "ord-7"
    assert store.holds() == []


def test_hold_resolves_when_the_site_shows_the_buy(tmp_path):
    bh, bx, multi, s, store, n, eng = two_markets(
        tmp_path, [ClaimOutcome(ClaimStatus.UNKNOWN, detail="timeout")])
    l = listing("bhopp", offer_id="o1")
    run(eng.attempt(l, W))
    assert run(eng.check_holds()) == {"bhopp"}
    bh.reconcile = [(ClaimStatus.CLAIMED, "ord-9")]
    assert run(eng.check_holds()) == set()
    row = store.recent_claims()[0]
    assert (row["status"], row["claim_id"]) == ("claimed", "ord-9")
    assert "▶️ Buying on BHOPP resumed" in n.titles()


def test_hold_times_out_and_keeps_counting_as_spent(tmp_path, store):
    bh = FakeAdapter("bhopp")
    s = make_settings(tmp_path, "claim")
    eng = ClaimEngine(s, store, bh, FakeNotifier())
    store.reserve_claim(idem_key="k", listing=listing(), daily_limit=None)
    store.update_claim("k", ClaimStatus.UNKNOWN)
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    store.set_hold(Hold("bhopp", "k", "Harvester", past, past))
    assert run(eng.check_holds()) == set()
    assert store.claim("k")["status"] == "timed_out"
    assert store.spent() == D("5.5")


def test_crash_mid_buy_holds_that_market_at_startup(tmp_path, store):
    s = make_settings(tmp_path, "claim")
    n = FakeNotifier()
    eng = ClaimEngine(s, store, FakeAdapter("bhopp"), n)
    store.reserve_claim(idem_key="k", listing=listing("bloxymarket"), daily_limit=None)
    eng.reconcile_on_startup()
    assert store.claim("k")["status"] == "unknown"
    assert [h.market for h in store.holds()] == ["bloxymarket"]
    assert any("Unfinished buy found at startup" in t for t in n.titles())


def test_overcharge_is_flagged(tmp_path):
    l = listing(price="5", qty=1)
    a = FakeAdapter("bhopp", outcomes=[ClaimOutcome(ClaimStatus.CLAIMED, "o", charged=D("6.5"),
                                                    quantity=1)])
    s, store, n, eng = setup(tmp_path, a)
    run(eng.attempt(l, W))
    assert any("price moved" in t for t in n.titles())
    assert D(store.recent_claims()[0]["price"]) == D("6.5")   # the ledger keeps the real cost


def test_alert_only_market_never_buys(tmp_path):
    bh = FakeAdapter("bhopp", claim=False)
    multi = MultiMarketAdapter({"bhopp": bh}, lambda: {"bhopp"})
    s, store, n, eng = setup(tmp_path, multi)
    assert run(eng.attempt(listing(), W)).status is ClaimStatus.SKIPPED and bh.calls == []
