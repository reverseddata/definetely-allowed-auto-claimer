"""Watcher: what counts as an opportunity, per-market isolation, the poll loop."""
import asyncio
from dataclasses import replace

from conftest import D, FakeAdapter, FakeNotifier, listing, make_settings, run
from mm2claimer.http import RateLimited, TransientError
from mm2claimer.marketplaces.multi import MultiMarketAdapter
from mm2claimer.models import ClaimOutcome, ClaimStatus
from mm2claimer.service import Watcher, is_trigger
from mm2claimer.store import Change, Store


def watcher(tmp_path, mode="claim", markets=None, **env):
    markets = markets or {"bhopp": FakeAdapter("bhopp"), "bloxymarket": FakeAdapter("bloxymarket")}
    multi = MultiMarketAdapter(markets, lambda: set(markets))
    s = make_settings(tmp_path, mode, extra_env={"POLL_INTERVAL_SECONDS": "0", **env})
    n = FakeNotifier()
    return Watcher(s, Store(":memory:"), multi, n), markets, n


def poll(w):
    """One loop pass, without sleeping."""
    return run(w._tick(0))


def test_triggers(tmp_path):
    s = make_settings(tmp_path)
    l = listing()
    for change in (Change.RESTOCKED, Change.PRICE_CHANGED, Change.MORE_STOCK):
        assert is_trigger(s, l, change)
    assert not is_trigger(s, l, Change.NEW)
    assert not is_trigger(s, l, Change.UNCHANGED)
    assert is_trigger(s, replace(l, extra={"new_release": True}), Change.NEW)
    assert is_trigger(make_settings(tmp_path, extra_env={"ACT_ON_FIRST_SIGHT": "1"}), l, Change.NEW)


def test_items_listed_at_startup_are_left_alone(tmp_path):
    w, m, n = watcher(tmp_path)
    m["bhopp"].ls = [listing()]
    poll(w)
    assert m["bhopp"].calls == []


def test_relisted_after_selling_out_is_bought(tmp_path):
    w, m, n = watcher(tmp_path)
    m["bhopp"].ls = [listing()]
    poll(w)
    m["bhopp"].ls = []                    # sold out: the complete poll marks it gone
    poll(w)
    m["bhopp"].ls = [listing()]
    poll(w)
    assert len(m["bhopp"].calls) == 1


def test_price_drop_into_range_is_bought(tmp_path):
    w, m, n = watcher(tmp_path)
    m["bhopp"].ls = [listing(price="7")]
    poll(w)
    m["bhopp"].ls = [listing(price="5")]
    poll(w)
    assert m["bhopp"].calls[0][0].price == D("5")


def test_keeps_buying_while_it_keeps_fitting(tmp_path):
    w, m, n = watcher(tmp_path)
    m["bhopp"].ls = [listing(price="7")]
    poll(w)
    for price in ("5", "5.5", "5.9"):     # each buy exposes the next offer up
        m["bhopp"].ls = [listing(price=price)]
        poll(w)
    m["bhopp"].ls = [listing(price="6.5")]
    poll(w)
    assert [c[0].price for c in m["bhopp"].calls] == [D("5"), D("5.5"), D("5.9")]


def test_units_left_after_a_capped_buy_are_bought_next_poll(tmp_path):
    w, m, n = watcher(tmp_path, MAX_CLAIM_QUANTITY="2", MAX_DAILY_SPEND="12")
    m["bhopp"].ls = [listing(price="7", qty=5)]
    poll(w)
    m["bhopp"].ls = [listing(price="3", qty=5)]
    for _ in range(4):
        poll(w)
    # 2 + 2 units, then the $12 cap allows no third buy (12 - 12 = 0).
    assert [c[0].quantity for c in m["bhopp"].calls] == [2, 2]


def test_one_site_entry_beats_both_sites(tmp_path):
    w, m, n = watcher(tmp_path, WATCH="Harvester<=6, Harvester@bloxymarket<=3")
    for a in m.values():
        a.ls = [listing(a.name, price="7")]
    poll(w)
    for a in m.values():
        a.ls = [listing(a.name, price="5")]
    poll(w)
    assert len(m["bhopp"].calls) == 1 and m["bloxymarket"].calls == []


def test_monitor_mode_only_alerts(tmp_path):
    w, m, n = watcher(tmp_path, mode="monitor")
    m["bhopp"].ls = [listing(price="7")]
    poll(w)
    m["bhopp"].ls = [listing(price="5")]
    poll(w)
    assert m["bhopp"].calls == [] and n.kinds() == ["available"]


def test_market_without_session_alerts_with_the_reason(tmp_path):
    markets = {"bhopp": FakeAdapter("bhopp", claim=False)}
    w, m, n = watcher(tmp_path, markets=markets)
    m["bhopp"].ls = [listing(price="7")]
    poll(w)
    m["bhopp"].ls = [listing(price="5")]
    poll(w)
    assert n.events[-1][0] == "available" and "no token" in n.events[-1][2]


def test_one_market_failing_does_not_stop_the_other(tmp_path):
    w, m, n = watcher(tmp_path)
    m["bloxymarket"].poll_exc = [RateLimited(60)]
    m["bhopp"].ls = [listing(price="7")]
    assert poll(w) is True
    assert w.adapter.rate_limited["bloxymarket"] == 60
    assert any("Rate limited by BloxyMarket" in t for t in n.titles())
    m["bhopp"].ls = [listing(price="5")]
    poll(w)                               # BloxyMarket is skipped while it cools down
    assert "bloxymarket" not in w.adapter.last_polled
    assert len(m["bhopp"].calls) == 1


def test_every_market_failing_backs_off(tmp_path):
    w, m, n = watcher(tmp_path)
    for a in m.values():
        a.poll_exc = [TransientError("down")]
    assert poll(w) is False


def test_down_and_recovered_alerts(tmp_path):
    markets = {"bhopp": FakeAdapter("bhopp")}
    w, m, n = watcher(tmp_path, markets=markets)
    m["bhopp"].poll_exc = [TransientError("down")] * 5
    for _ in range(5):
        w.adapter._next_ok.clear()        # skip the per-market backoff in the test
        poll(w)
    assert any("BHOPP unreachable" in t for t in n.titles())
    w.adapter._next_ok.clear()
    poll(w)
    assert any("BHOPP reachable again" in t for t in n.titles())


def test_held_market_is_not_polled(tmp_path):
    bh = FakeAdapter("bhopp", outcomes=[ClaimOutcome(ClaimStatus.UNKNOWN, detail="timeout")])
    w, m, n = watcher(tmp_path, markets={"bhopp": bh, "bloxymarket": FakeAdapter("bloxymarket")})
    for a in m.values():
        a.ls = [listing(a.name, price="7")]
    poll(w)
    for a in m.values():
        a.ls = [listing(a.name, price="5")]
    poll(w)                               # BHOPP unclear -> held; BloxyMarket bought
    assert w.held == set() and [h.market for h in w.store.holds()] == ["bhopp"]
    poll(w)
    assert w.held == {"bhopp"} and "bhopp" not in w.adapter.last_polled
    assert len(m["bloxymarket"].calls) == 1


def test_pause_from_discord_stops_polling(tmp_path):
    w, m, n = watcher(tmp_path, DISCORD_BOT_TOKEN="x" * 20, DISCORD_MENTION_USER_ID="1")
    w.watchlist.set_paused(True)
    assert poll(w) is None and w.adapter.last_polled == set()


def test_slow_market_does_not_hold_the_fast_one_back(tmp_path):
    class Slow(FakeAdapter):
        async def get_listings(self, only=None):
            self.polls = getattr(self, "polls", 0) + 1
            await asyncio.sleep(0.4)
            return []

    class Fast(FakeAdapter):
        async def get_listings(self, only=None):
            self.polls = getattr(self, "polls", 0) + 1
            await asyncio.sleep(0.01)
            return [listing(price="7")]

    markets = {"bhopp": Fast("bhopp"), "bloxymarket": Slow("bloxymarket")}
    w, m, n = watcher(tmp_path, markets=markets)

    async def go():
        task = asyncio.create_task(w.run())
        await asyncio.sleep(0.6)
        w.stop()
        await asyncio.wait_for(task, 5)
    run(go())
    assert m["bloxymarket"].polls <= 2 and m["bhopp"].polls >= 10
    assert any("claimer started" in t for t in n.titles())


def test_status_file(tmp_path):
    w, m, n = watcher(tmp_path)
    poll(w)
    import json
    data = json.loads(w.s.status_file.read_text())
    assert set(data["markets"]) == {"bhopp", "bloxymarket"} and data["holds"] == []
