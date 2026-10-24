"""Discord slash commands.

    /watch add item market max min_qty max_qty   watch an item, or change its limits
    /watch remove item market                    stop watching it
    /watch list                                  what is watched
    /watch pause | resume                        stop or restart sniping
    /limits                                      show or set the spending limits
    /market                                      which marketplaces are polled
    /balance                                     accounts and balances on both sites
    /status                                      mode, markets, unclear buys, last poll

Only the Discord user in DISCORD_MENTION_USER_ID may run them; everyone else is
refused. Replies are ephemeral (only that user sees them). Changes are stored in
the database and apply on the next poll, without a restart or redeploy.

`Commands` holds all logic and knows nothing about Discord, so it is tested
directly. `build_tree` / `start_bot` are the thin discord.py layer.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from decimal import Decimal, InvalidOperation
from typing import Literal

from .config import MARKETS, WatchEntry, limit_problems, parse_markets, parse_watch_market
from .http import AuthError, MarketplaceError
from .models import norm_name
from .notify import _md, label, money
from .store import utc_day
from .watchlist import OFF

log = logging.getLogger(__name__)
MAX_REPLY = 1900
# Discord wants an acknowledgement within 3 s and the answer within 15 min. Every
# command acknowledges first, then gets this long before it answers with a timeout
# message instead. The site calls inside have budgets of their own, below it.
COMMAND_TIMEOUT = 90.0
LOOKUP_TIMEOUT = 45.0       # /watch add's name check (BloxyMarket spaces requests)
BALANCE_TIMEOUT = 30.0      # per site, both sites read at once
AUTOCOMPLETE_TIMEOUT = 2.0  # autocomplete has only the 3 s window, no deferral
LIMIT_NAMES = {"daily_spend": "max_daily_spend", "max_price": "max_claim_price",
               "max_quantity": "max_claim_quantity"}


def _price(value, label_: str) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        d = Decimal(str(value).strip())
    except InvalidOperation:
        raise ValueError(f"{label_} must be a number") from None
    if not d.is_finite() or d < 0:
        raise ValueError(f"{label_} must be a positive number")
    return d


def _where(market: str) -> str:
    return "both sites" if market == "both" else label(market)


def describe(w: WatchEntry) -> str:
    parts = [f"on {_where(w.market)}",
             f"≤ ${w.max_price}" if w.max_price is not None
             else "any price (MAX_CLAIM_PRICE applies)"]
    if w.min_qty > 1:
        parts.append(f"at least {w.min_qty} at a time")
    if w.max_qty:
        parts.append(f"at most {w.max_qty} at a time")
    return f"**{_md(w.item)}** " + ", ".join(parts)


class Commands:
    def __init__(self, settings, store, adapter, watcher):
        self.s = settings
        self.store = store
        self.adapter = adapter
        self.watcher = watcher
        self.watchlist = watcher.watchlist
        self.limits = watcher.limits

    def allowed(self, user_id) -> bool:
        return bool(self.s.discord_owner_id) and str(user_id) == self.s.discord_owner_id

    def _sub(self, market: str):
        pick = getattr(self.adapter, "adapter", None)
        return pick(market) if pick else self.adapter

    # ---------------------------------------------------------------- watch

    async def add(self, item: str, market: str = "both", max_price=None,
                  min_qty: int | None = None, max_qty: int | None = None) -> str:
        where = parse_watch_market(market)
        if where is None:
            return "Market must be both, bhopp or bloxymarket."
        item = (item or "").strip()[:100]
        key = norm_name(item)
        if not key:
            return "Give an item name."
        try:
            mx = _price(max_price, "max")
        except ValueError as e:
            return f"Not added: {e}."
        if min_qty is not None and min_qty < 1:
            return "Not added: min_qty must be 1 or more."
        if max_qty is not None and max_qty < max(1, min_qty or 1):
            return "Not added: max_qty must be at least 1 and at least min_qty."
        if mx is not None and mx == 0:
            return "Not added: max 0 would never match. Leave it empty for no item limit."
        try:
            found = await asyncio.wait_for(
                self.adapter.lookup(item, where) if hasattr(self.adapter, "adapters")
                else self.adapter.lookup(item), LOOKUP_TIMEOUT)
        except asyncio.TimeoutError:
            return (f"The market took over {LOOKUP_TIMEOUT:g}s to answer the name check. "
                    "Nothing changed; try again.")
        except MarketplaceError as e:
            return f"Could not reach the market to check the name ({e}). Nothing changed."
        hits = [l for l in found if key in (norm_name(l.name), norm_name(l.slug))]
        note = ""
        if not hits:
            # Not listed now (sold out, or not released yet): watch the name as typed,
            # so it is bought the moment it is listed.
            close = sorted({l.name for l in found})[:8]
            hint = (" Similar names listed now: " + ", ".join(_md(n) for n in close) + "."
                    ) if close else ""
            note = (f"\n⚠️ Nothing called **{_md(item)}** is listed on {_where(where)} right "
                    f"now. It is watched anyway and bought when it appears, if the name "
                    f"matches exactly (chromas are \"Chroma <name>\").{hint}")
        name = hits[0].name if hits else item
        entry = WatchEntry(name, where, mx, key=norm_name(name), min_qty=min_qty or 1,
                           max_qty=max_qty)
        # The database is shared with the poll loops: never wait on it in the event loop.
        problem, existed = await asyncio.to_thread(self._save, entry)
        if problem:
            return problem
        now = [f"{label(l.marketplace)} ${l.price} ({l.quantity} at that price)"
               for l in hits]
        return (("Updated " if existed else "Watching ") + describe(entry)
                + (f"\nRight now: {'; '.join(now)}" if now else "")
                + note + "\nApplies on the next poll.")

    def _save(self, entry: WatchEntry) -> tuple[str, bool]:
        """(refusal or "", whether it replaced an entry). Runs in a worker thread."""
        if self.s.mode == "claim":
            problems = limit_problems(entry, self.limits.max_claim_price)
            if problems:
                return "Not added. " + " ".join(problems), False
        existed = any(w.watch_id == entry.watch_id for w in self.store.watch_entries())
        self.store.put_watch(entry)
        return "", existed

    def remove(self, item: str, market: str | None = None) -> str:
        key = norm_name(item or "")
        where = parse_watch_market(market) if market else None
        gone = []
        for w in self.store.watch_entries():
            if (w.key == key or norm_name(w.item) == key) and where in (None, w.market):
                self.store.remove_watch(w.watch_id)
                gone.append(w)
        if gone:
            return "Stopped watching " + ", ".join(f"**{_md(w.item)}** on {_where(w.market)}"
                                                   for w in gone) + "."
        return f"**{_md(item)}** is not on the list. Use /watch list."

    def list_text(self) -> str:
        entries = self.store.watch_entries()
        head = "⏸️ Sniping is paused (/watch resume).\n" if self.watchlist.paused else ""
        if not entries:
            return head + "Nothing is watched. Add something with /watch add."
        lines = [f"• {describe(w)}" for w in entries]
        return head + f"Watching {len(entries)} item(s), mode **{self.s.mode}**:\n" \
            + "\n".join(lines)

    def market(self, choice: str | None = None) -> str:
        """Show, or switch, which marketplaces are polled. No restart needed."""
        if choice:
            markets = parse_markets(choice)
            if markets is None:
                return "Choose bhopp, bloxymarket or both."
            self.watchlist.set_markets(markets)
            head = "Now polling "
        else:
            markets = self.watchlist.markets()
            head = "Polling "
        lines = [head + " and ".join(label(m) for m in sorted(markets)) + "."]
        for m in sorted(markets):
            a = self._sub(m)
            if a is not None and not a.capabilities.claim and self.s.mode == "claim":
                lines.append(f"{label(m)} is alert only: {a.capabilities.claim_unavailable_reason}.")
        errors = getattr(self.adapter, "errors", {})
        for m in sorted(markets):
            if errors.get(m):
                lines.append(f"⚠️ {label(m)} last poll failed: {errors[m][:150]}")
        return "\n".join(lines)

    def pause(self) -> str:
        self.watchlist.set_paused(True)
        return "⏸️ Paused. Nothing is polled or bought until /watch resume."

    def resume(self) -> str:
        self.watchlist.set_paused(False)
        return "▶️ Resumed. Watching again from the next poll."

    def item_names(self, current: str, limit: int = 25) -> list[str]:
        """Autocomplete: names the sites know, filtered by what was typed."""
        want = norm_name(current or "")
        names = self.adapter.item_names() if hasattr(self.adapter, "item_names") else []
        hits = [n for n in names if want in norm_name(n)] if want else names
        return hits[:limit]

    def watched_names(self, current: str) -> list[str]:
        want = norm_name(current or "")
        return sorted({w.item for w in self.store.watch_entries()
                       if want in norm_name(w.item)})[:25]

    # ---------------------------------------------------------------- limits

    def set_limits(self, daily_spend: str | None = None, max_price: str | None = None,
                   max_quantity: str | None = None) -> str:
        """Each value: a number, "off" (no limit) or "default" (back to .env)."""
        given = {"daily_spend": daily_spend, "max_price": max_price,
                 "max_quantity": max_quantity}
        changes = {}
        for opt, raw in given.items():
            if raw is None or not str(raw).strip():
                continue
            v = str(raw).strip().lower()
            if v in ("default", "reset"):
                changes[opt] = None
            elif v in ("off", "none", "0"):
                changes[opt] = OFF
            else:
                try:
                    if opt == "max_quantity":
                        n = int(v)
                        if n < 1:
                            raise ValueError
                        changes[opt] = str(n)
                    else:
                        d = _price(v, opt)
                        changes[opt] = str(d)
                except ValueError:
                    return (f"Not changed: {opt} must be "
                            + ("a whole number" if opt == "max_quantity" else "a number")
                            + ", \"off\" or \"default\".")
        if changes.get("max_price") == OFF and self.s.mode == "claim":
            unlimited = [w.item for w in self.store.watch_entries() if w.max_price is None]
            if unlimited:
                return ("Not changed: these watches have no max of their own and would be "
                        "bought at any price: " + ", ".join(_md(n) for n in unlimited[:10])
                        + ". Give them a max first (/watch add ... max:).")
        for opt, value in changes.items():
            self.limits.set(LIMIT_NAMES[opt], value)
        return ("Updated.\n" if changes else "") + self.limits_text()

    def limits_text(self) -> str:
        L = self.limits

        def src(name):
            return " (set here)" if L.overridden(name) else " (from .env)"
        spend = self.store.spent(utc_day())
        daily = L.max_daily_spend
        lines = [
            f"Daily spend cap: {'$' + str(daily) if daily is not None else 'off'}"
            f"{src('max_daily_spend')}, spent today (UTC): ${spend}",
            f"Max price per unit: "
            f"{'$' + str(L.max_claim_price) if L.max_claim_price is not None else 'off'}"
            f"{src('max_claim_price')}",
            f"Max units per buy: {L.max_claim_quantity or 'off (all the balance allows)'}"
            f"{src('max_claim_quantity')}",
            "Buying also stops on a site when its balance runs out.",
        ]
        return "\n".join(lines)

    # ---------------------------------------------------------------- info

    async def balance(self) -> str:
        """Both sites at once, each within BALANCE_TIMEOUT: one slow site never makes
        the other's answer late."""
        markets = [m for m in MARKETS if self._sub(m) is not None]
        lines = await asyncio.gather(*(self._balance_line(m) for m in markets))
        return "\n".join(lines) or "No marketplaces configured."

    async def _balance_line(self, m: str) -> str:
        a = self._sub(m)
        if not a.capabilities.claim:
            return f"**{label(m)}**: no session ({a.capabilities.claim_unavailable_reason})."

        async def read():
            return await a.read_account(), await a.get_balance()
        try:
            account, value = await asyncio.wait_for(read(), BALANCE_TIMEOUT)
        except asyncio.TimeoutError:
            return f"**{label(m)}**: no answer within {BALANCE_TIMEOUT:g}s. Try again."
        except AuthError:
            return f"**{label(m)}**: the site rejected the session. Paste a fresh one into .env."
        except MarketplaceError as e:
            return f"**{label(m)}**: could not read the balance: {e}"
        self.watcher.engine.balances[m] = (value, time.monotonic())
        line = f"**{label(m)}**: account **{_md(account)}**, balance {money(value, a.unit)}."
        warning = a.session_warning()
        return line + (f"\n  🔑 {warning}" if warning else "")

    def status(self) -> str:
        markets = self.watchlist.markets()
        lines = [f"Mode **{self.s.mode}**, watching {len(self.store.watch_entries())} item(s), "
                 f"markets: {', '.join(label(m) for m in sorted(markets))}.",
                 "⏸️ Paused from Discord." if self.watchlist.paused else "Running.",
                 f"Spent today (UTC): ${self.store.spent(utc_day())}"
                 + (f" of ${self.limits.max_daily_spend}"
                    if self.limits.max_daily_spend is not None else "") + "."]
        for h in self.store.holds():
            lines.append(f"⏳ {label(h.market)}: checking whether the buy of **{_md(h.name)}** "
                         "went through; buying there is paused until then.")
        for m in sorted(markets):
            a = self._sub(m)
            if a is None:
                continue
            st = a.status
            bits = [f"**{label(m)}**:"]
            if st.last_success:
                bits.append(f"last poll OK <t:{int(st.last_success.timestamp())}:R>")
            if st.last_error:
                bits.append(f"last error: {st.last_error[:150]}")
            bal = self.watcher.engine.balances.get(m)
            if bal:
                bits.append(f"balance {money(bal[0], a.unit)}")
            if not a.capabilities.claim:
                bits.append("alert only (no session)")
            lines.append(" ".join(bits))
        return "\n".join(lines)


# -------------------------------------------------------------------- discord.py layer


def _clip(text: str) -> str:
    return text if len(text) <= MAX_REPLY else text[:MAX_REPLY - 1] + "…"


async def run_command(interaction, allowed: bool, work, *, timeout: float = COMMAND_TIMEOUT):
    """Answer one slash command so it can never show "The application did not respond":
    acknowledge first (Discord's 3 s deadline), then do the work within `timeout`, and
    always send something back: the answer, a timeout note, or the error."""
    import discord

    if not allowed:
        log.warning("refused a Discord command from user %s", interaction.user.id)
        try:
            await interaction.response.send_message("You are not allowed to use this bot.",
                                                    ephemeral=True)
        except discord.HTTPException as e:
            log.warning("could not refuse a Discord command: %s", e)
        return
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
    except discord.HTTPException as e:
        # Only if Discord itself was too slow to deliver the command; nothing to answer.
        log.warning("could not acknowledge a Discord command: %s", e)
        return
    try:
        text = await asyncio.wait_for(work(), timeout)
    except asyncio.TimeoutError:
        text = (f"That took over {timeout:g}s, so the bot stopped waiting. "
                "Nothing else changed; try again.")
    except Exception as e:  # noqa: BLE001 - the user must hear about it, not see a dead spinner
        log.exception("Discord command failed")
        text = f"That failed: {type(e).__name__}: {e}"
    try:
        await interaction.followup.send(_clip(text), ephemeral=True)
    except discord.HTTPException as e:
        log.warning("could not send a Discord reply: %s", e)


async def _choices(fn, current: str) -> list[str]:
    """Autocomplete within its 3 s window: off the event loop, empty if it is late."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn, current), AUTOCOMPLETE_TIMEOUT)
    except Exception as e:  # noqa: BLE001 - an empty list beats an error popup
        log.debug("autocomplete failed: %s", e)
        return []


def build_tree(client, commands: Commands, guild=None):
    """Register the slash commands on a new CommandTree. No network."""
    import discord
    from discord import app_commands

    tree = app_commands.CommandTree(client)

    def go(interaction, fn, *args):
        """Run a Commands method: async ones as they are, the others (database only)
        in a worker thread, so the event loop never waits on the database."""
        if inspect.iscoroutinefunction(fn):
            def work():
                return fn(*args)
        else:
            def work():
                return asyncio.to_thread(fn, *args)
        return run_command(interaction, commands.allowed(interaction.user.id), work)

    async def names(interaction, fn, current):
        if not commands.allowed(interaction.user.id):
            return []
        return [app_commands.Choice(name=n[:100], value=n[:100])
                for n in await _choices(fn, current)]

    group = app_commands.Group(name="watch", description="Items the sniper watches")

    @group.command(name="add", description="Watch an item, or change its limits")
    @app_commands.rename(max_price="max")
    @app_commands.describe(item="item name, e.g. Harvester or Chroma Seer",
                           market="which site (default both)",
                           max_price="most you pay per unit, in dollars",
                           min_qty="only buy when at least this many fit",
                           max_qty="most units per buy (default: all the balance allows)")
    async def add(interaction: discord.Interaction, item: str,
                  market: Literal["both", "bhopp", "bloxymarket"] = "both",
                  max_price: float | None = None,
                  min_qty: int | None = None, max_qty: int | None = None):
        await go(interaction, commands.add, item, market, max_price, min_qty, max_qty)

    @add.autocomplete("item")
    async def add_item(interaction: discord.Interaction, current: str):
        return await names(interaction, commands.item_names, current)

    @group.command(name="remove", description="Stop watching an item")
    @app_commands.describe(market="only on this site (default: every entry for the item)")
    async def remove(interaction: discord.Interaction, item: str,
                     market: Literal["both", "bhopp", "bloxymarket"] | None = None):
        await go(interaction, commands.remove, item, market)

    @remove.autocomplete("item")
    async def remove_item(interaction: discord.Interaction, current: str):
        return await names(interaction, commands.watched_names, current)

    @group.command(name="list", description="Show the watched items")
    async def list_(interaction: discord.Interaction):
        await go(interaction, commands.list_text)

    @group.command(name="pause", description="Pause sniping (no polling, no buys)")
    async def pause(interaction: discord.Interaction):
        await go(interaction, commands.pause)

    @group.command(name="resume", description="Resume sniping")
    async def resume(interaction: discord.Interaction):
        await go(interaction, commands.resume)

    tree.add_command(group, guild=guild)

    @tree.command(name="limits", description="Show or set the spending limits", guild=guild)
    @app_commands.describe(daily_spend="dollars per UTC day, both sites (off, default)",
                           max_price="most per unit on any buy (off, default)",
                           max_quantity="most units per buy (off, default)")
    async def limits(interaction: discord.Interaction, daily_spend: str | None = None,
                     max_price: str | None = None, max_quantity: str | None = None):
        await go(interaction, commands.set_limits, daily_spend, max_price, max_quantity)

    @tree.command(name="balance", description="Accounts and balances on BHOPP and BloxyMarket",
                  guild=guild)
    async def balance(interaction: discord.Interaction):
        await go(interaction, commands.balance)

    @tree.command(name="market", description="Which marketplaces to snipe on", guild=guild)
    @app_commands.describe(choice="leave empty to see the current choice")
    async def market(interaction: discord.Interaction,
                     choice: Literal["bhopp", "bloxymarket", "both"] | None = None):
        await go(interaction, commands.market, choice)

    @tree.command(name="status", description="Sniper status", guild=guild)
    async def status(interaction: discord.Interaction):
        await go(interaction, commands.status)

    @tree.error
    async def on_error(interaction: discord.Interaction, error):
        # Argument conversion errors and the like, raised before a command body runs.
        log.warning("Discord command error: %s", error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(_clip(f"That failed: {error}"), ephemeral=True)
            else:
                await interaction.response.send_message(_clip(f"That failed: {error}"),
                                                        ephemeral=True)
        except discord.HTTPException:
            pass

    return tree


async def start_bot(settings, commands: Commands, notifier):
    """Connect the bot in the background. A bad token or an outage never stops the
    sniper itself: it is logged and reported through the webhook."""
    import discord

    # Slash commands need no privileged intents; "guilds" only keeps discord.py quiet.
    client = discord.Client(intents=discord.Intents(guilds=True))
    guild = discord.Object(id=int(settings.discord_guild_id)) if settings.discord_guild_id \
        else None
    tree = build_tree(client, commands, guild)

    @client.event
    async def on_ready():
        try:
            synced = await (tree.sync(guild=guild) if guild else tree.sync())
            log.info("discord bot online as %s with %d commands%s", client.user, len(synced),
                     "" if guild else " (global commands can take a few minutes to appear)")
        except Exception as e:  # noqa: BLE001
            log.error("could not register Discord commands: %s", e)

    async def runner():
        try:
            await client.start(settings.discord_bot_token)
        except discord.LoginFailure:
            log.error("Discord rejected DISCORD_BOT_TOKEN")
            notifier.system("Discord bot token rejected",
                            "The commands are off. Check DISCORD_BOT_TOKEN.", "error")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Discord bot stopped")

    client.mm2_task = asyncio.create_task(runner(), name="discord-bot")
    return client
