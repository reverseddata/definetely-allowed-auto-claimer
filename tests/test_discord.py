"""Discord commands: logic, authorization, persistence. No Discord connection is made."""
import asyncio

import pytest

from conftest import D, FakeAdapter, FakeNotifier, listing, make_settings, run
from mm2claimer.config import ConfigError, load_settings
from mm2claimer.discord_bot import Commands, build_tree
from mm2claimer.http import AuthError, TransientError
from mm2claimer.marketplaces.multi import MultiMarketAdapter
from mm2claimer.service import Watcher
from mm2claimer.store import Store

OWNER = "123456789"


def setup(tmp_path, mode="claim", watch="", **env):
    base = {"DISCORD_BOT_TOKEN": "bot-token-not-real-123", "DISCORD_MENTION_USER_ID": OWNER}
    base.update(env)
    s = make_settings(tmp_path, mode, extra_env=base, watch=watch)
    markets = {"bhopp": FakeAdapter("bhopp", [listing("bhopp")]),
               "bloxymarket": FakeAdapter("bloxymarket", [listing("bloxymarket", price="5.75")])}
    multi = MultiMarketAdapter(markets, lambda: {"bhopp", "bloxymarket"})
    w = Watcher(s, Store(":memory:"), multi, FakeNotifier())
    return s, w, markets, Commands(s, w.store, multi, w)


def test_bot_token_needs_an_owner(tmp_path):
    with pytest.raises(ConfigError) as e:
        make_settings(tmp_path, "monitor", extra_env={"DISCORD_BOT_TOKEN": "x" * 20})
    assert "DISCORD_MENTION_USER_ID" in " ".join(e.value.problems)


def test_empty_watchlist_is_fine_with_a_bot(tmp_path):
    s = load_settings(None, env={"DATA_DIR": str(tmp_path), "DISCORD_BOT_TOKEN": "x" * 20,
                                 "DISCORD_MENTION_USER_ID": OWNER})
    assert s.watch == []


def test_only_the_owner(tmp_path):
    s, w, m, c = setup(tmp_path)
    assert c.allowed(int(OWNER)) and not c.allowed(42)


def test_add_shows_both_sites_and_persists(tmp_path):
    s, w, m, c = setup(tmp_path)
    text = run(c.add("harvester", "both", 6))
    assert "Watching **Harvester** on both sites" in text
    assert "BHOPP $5.5" in text and "BloxyMarket $5.75" in text
    assert [(e.item, e.market, e.max_price) for e in w.watchlist.entries()] == [
        ("Harvester", "both", D("6"))]
    assert "Updated" in run(c.add("Harvester", "both", 5))


def test_per_site_entries_and_remove(tmp_path):
    s, w, m, c = setup(tmp_path)
    run(c.add("Harvester", "bhopp", 6))
    run(c.add("Harvester", "bloxymarket", 4))
    assert len(w.store.watch_entries()) == 2
    assert "on BHOPP" in c.remove("harvester", "bhopp")
    assert [e.market for e in w.store.watch_entries()] == ["bloxymarket"]
    c.remove("Harvester")
    assert w.store.watch_entries() == []


def test_unlisted_item_is_watched_anyway(tmp_path):
    s, w, m, c = setup(tmp_path)
    text = run(c.add("Brand New Knife", "both", 3))
    assert "watched anyway" in text and w.store.watch_entries()[0].item == "Brand New Knife"


def test_claim_mode_refuses_unlimited(tmp_path):
    s, w, m, c = setup(tmp_path)
    assert "any price" in run(c.add("Harvester"))
    assert w.store.watch_entries() == []
    w.limits.set("max_claim_price", "10")
    assert "Watching" in run(c.add("Harvester"))


def test_bad_inputs(tmp_path):
    s, w, m, c = setup(tmp_path)
    assert "Market must be" in run(c.add("Harvester", "ebay", 1))
    assert "never match" in run(c.add("Harvester", "both", 0))
    assert "max_qty" in run(c.add("Harvester", "both", 1, min_qty=3, max_qty=2))


def test_add_survives_an_outage(tmp_path):
    s, w, m, c = setup(tmp_path)

    async def boom(item):
        raise TransientError("timeout")
    for a in m.values():
        a.lookup = boom
    assert "Nothing changed" in run(c.add("Harvester", "both", 6))
    assert w.store.watch_entries() == []


def test_limits_set_off_default(tmp_path):
    s, w, m, c = setup(tmp_path, MAX_DAILY_SPEND="50")
    assert "Daily spend cap: $50 (from .env)" in c.limits_text()
    text = c.set_limits(daily_spend="20", max_quantity="3")
    assert "Daily spend cap: $20 (set here)" in text and "Max units per buy: 3" in text
    assert w.limits.max_daily_spend == D("20") and w.limits.max_claim_quantity == 3
    c.set_limits(daily_spend="off")
    assert w.limits.max_daily_spend is None
    c.set_limits(daily_spend="default")
    assert w.limits.max_daily_spend == D("50")
    assert "must be" in c.set_limits(max_quantity="lots")


def test_limits_wont_unlock_unlimited_watches(tmp_path):
    s, w, m, c = setup(tmp_path, MAX_CLAIM_PRICE="10")
    run(c.add("Harvester"))
    assert "any price" in c.set_limits(max_price="off")
    assert w.limits.max_claim_price == D("10")


def test_balance_for_both_sites(tmp_path):
    s, w, m, c = setup(tmp_path)
    m["bloxymarket"].balance = AuthError("401")
    text = run(c.balance())
    assert "**BHOPP**: account **TestBuyer**, balance $100" in text
    assert "BloxyMarket**: the site rejected" in text
    assert w.engine.balances["bhopp"][0] == D("100")


def test_market_switch_and_status(tmp_path):
    s, w, m, c = setup(tmp_path)
    assert "Now polling BHOPP." in c.market("bhopp")
    assert w.watchlist.markets() == {"bhopp"}
    assert "Choose" in c.market("ebay")
    assert "Mode **claim**" in c.status()


def test_pause_resume(tmp_path):
    s, w, m, c = setup(tmp_path)
    c.pause()
    assert w.watchlist.paused and "paused" in c.list_text()
    c.resume()
    assert not w.watchlist.paused


def test_tree_registers_commands(tmp_path):
    import discord
    s, w, m, c = setup(tmp_path)
    client = discord.Client(intents=discord.Intents(guilds=True))
    tree = build_tree(client, c)
    names = {cmd.name for cmd in tree.get_commands()}
    assert names == {"watch", "limits", "balance", "market", "status"}
    watch = tree.get_command("watch")
    assert {sub.name for sub in watch.commands} == {"add", "remove", "list", "pause", "resume"}


# ---------------------------------------------------------------- never time out


class FakeInteraction:
    """Records what the bot sends, in order, and when (seconds after creation)."""

    class _User:
        id = int(OWNER)

    def __init__(self):
        import time as _t
        self.t0 = _t.monotonic()
        self.sent = []
        self.user = self._User()
        outer = self

        class Response:
            done = False

            async def defer(self, **kw):
                outer.sent.append(("defer", round(_t.monotonic() - outer.t0, 2)))
                Response.done = True

            async def send_message(self, text, **kw):
                outer.sent.append(("message", text))
                Response.done = True

            def is_done(self):
                return Response.done

        class Followup:
            async def send(self, text, **kw):
                outer.sent.append(("followup", text))

        self.response, self.followup = Response(), Followup()


def test_acknowledges_before_doing_any_work():
    from mm2claimer.discord_bot import run_command
    i = FakeInteraction()

    async def slow():
        await asyncio.sleep(0.3)
        return "done"
    run(run_command(i, True, slow))
    assert i.sent[0] == ("defer", 0.0) and i.sent[1] == ("followup", "done")


def test_slow_work_answers_with_a_timeout_note():
    from mm2claimer.discord_bot import run_command
    i = FakeInteraction()

    async def stuck():
        await asyncio.sleep(10)
    run(run_command(i, True, stuck, timeout=0.2))
    assert i.sent[0][0] == "defer" and "took over 0.2s" in i.sent[1][1]


def test_errors_are_answered_not_swallowed():
    from mm2claimer.discord_bot import run_command
    i = FakeInteraction()

    async def boom():
        raise RuntimeError("database is locked")
    run(run_command(i, True, boom))
    assert i.sent[-1] == ("followup", "That failed: RuntimeError: database is locked")


def test_strangers_get_refused_without_any_work():
    from mm2claimer.discord_bot import run_command
    i = FakeInteraction()
    called = []

    async def work():
        called.append(1)
    run(run_command(i, False, work))
    assert called == [] and i.sent == [("message", "You are not allowed to use this bot.")]


def test_slow_site_lookup_gives_up_in_time(tmp_path, monkeypatch):
    import mm2claimer.discord_bot as bot
    monkeypatch.setattr(bot, "LOOKUP_TIMEOUT", 0.2)
    s, w, m, c = setup(tmp_path)

    async def hang(item):
        await asyncio.sleep(10)
    for a in m.values():
        a.lookup = hang
    assert "took over 0.2s" in run(c.add("Harvester", "both", 6))
    assert w.store.watch_entries() == []


def test_one_slow_site_does_not_delay_the_other_balance(tmp_path, monkeypatch):
    import mm2claimer.discord_bot as bot
    monkeypatch.setattr(bot, "BALANCE_TIMEOUT", 0.2)
    s, w, m, c = setup(tmp_path)

    async def hang():
        await asyncio.sleep(10)
    m["bloxymarket"].get_balance = hang
    text = run(asyncio.wait_for(c.balance(), 2))
    assert "balance $100" in text and "no answer within 0.2s" in text


def test_late_autocomplete_returns_nothing(monkeypatch):
    import time as _t
    import mm2claimer.discord_bot as bot
    monkeypatch.setattr(bot, "AUTOCOMPLETE_TIMEOUT", 0.1)
    assert run(bot._choices(lambda cur: _t.sleep(0.5) or ["x"], "h")) == []
    assert run(bot._choices(lambda cur: ["Harvester"], "h")) == ["Harvester"]


def test_every_command_goes_through_the_safe_runner(tmp_path, monkeypatch):
    """Each registered command defers first and always answers."""
    import discord
    import mm2claimer.discord_bot as bot
    s, w, m, c = setup(tmp_path)
    tree = build_tree(discord.Client(intents=discord.Intents(guilds=True)), c)
    cmds = [cmd for cmd in tree.get_commands() if not hasattr(cmd, "commands")]
    cmds += list(tree.get_command("watch").commands)
    args = {"add": {"item": "Harvester"}, "remove": {"item": "Harvester"}}
    for cmd in cmds:
        i = FakeInteraction()
        run(cmd.callback(i, **args.get(cmd.name, {})))
        assert i.sent[0][0] == "defer" and i.sent[-1][0] == "followup", cmd.name
