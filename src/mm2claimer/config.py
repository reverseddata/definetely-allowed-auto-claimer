"""Configuration: secrets and headline settings from environment variables (.env),
the watchlist from a TOML file. Validated at startup; every problem is
reported at once.

Precedence: environment variable > config file > default.
Prices are US dollars on both sites (BHOPP shows dollars, BloxyMarket coins are $1).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from .models import norm_name
from .notify import register_secret

MODES = ("monitor", "claim")
MARKETS = ("bhopp", "bloxymarket")
# Which marketplaces a watch entry applies to.
WATCH_MARKETS = ("both", "bhopp", "bloxymarket")


def parse_markets(value: str) -> set[str] | None:
    """"bhopp", "bloxymarket", "both" or "bhopp,bloxymarket" -> set; None if invalid.
    Empty means both."""
    v = (value or "").strip().lower()
    if v in ("", "both", "all"):
        return set(MARKETS)
    parts = {p.strip() for p in v.replace("+", ",").split(",") if p.strip()}
    return parts if parts and parts <= set(MARKETS) else None


def parse_watch_market(value: str) -> str | None:
    v = (value or "both").strip().lower()
    v = "both" if v == "all" else v
    return v if v in WATCH_MARKETS else None


MIN_POLL_INTERVAL = 0.0
WEBHOOK_RE = re.compile(
    r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


class ConfigError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("invalid configuration:\n  - " + "\n  - ".join(problems))


def limit_problems(w: "WatchEntry", max_claim_price: Decimal | None) -> list[str]:
    """Claim mode refuses an entry that could buy at any price. Shared by config
    loading and the Discord /watch add command."""
    if w.max_price is None and max_claim_price is None:
        return [f"claim mode: {w.item!r} would be bought at any price. Set a max price "
                "(max_price, Item<=N in WATCH, max: in /watch add) or MAX_CLAIM_PRICE"]
    return []


# "Item", "Item<=3.5", "Item@bhopp<=3.5", "Item<=3.5 x2", "Item@bloxymarket<=1 x1-4"
# (at least 1, at most 4 units per buy)
WATCH_ENTRY_RE = re.compile(r"^(?P<item>[^@<>=]+?)\s*(?:@\s*(?P<market>[A-Za-z]+))?\s*"
                            r"(?:<=\s*(?P<max>[^<>=\sx]+)\s*)?"
                            r"(?:x\s*(?P<qty>\d+)(?:\s*-\s*(?P<maxqty>\d+))?)?\s*$")


@dataclass(frozen=True)
class WatchEntry:
    item: str                       # as written by the operator
    market: str = "both"            # both / bhopp / bloxymarket
    max_price: Decimal | None = None  # buy only at or below this (USD per unit)
    key: str = ""                   # normalized name or slug used for matching
    min_qty: int = 1                # buy only when at least this many units fit
    max_qty: int | None = None      # most units per buy; None = all the balance allows

    @property
    def watch_id(self) -> str:
        """One entry per item and market: Harvester can have a BHOPP price and a
        different BloxyMarket price."""
        return self.key if self.market == "both" else f"{self.market}:{self.key}"

    def matches(self, name: str, slug: str, market: str) -> bool:
        return (self.market in (market, "both")
                and self.key in (norm_name(name), norm_name(slug)))

    def price_ok(self, price: Decimal) -> bool:
        return self.max_price is None or price <= self.max_price


@dataclass
class Settings:
    mode: str = "monitor"
    data_dir: Path = Path("data")
    log_dir: Path = Path("logs")
    log_level: str = "INFO"
    log_json: bool = True

    discord_webhook_url: str = ""
    discord_username: str = "MM2 Claimer"
    discord_mention: str = ""                   # "<@id>" pinged on buys
    discord_owner_id: str = ""                  # the only user allowed to run commands
    markets: set = field(default_factory=lambda: set(MARKETS))  # polled marketplaces
    # Discord bot for /watch commands. Optional; unset = webhook alerts only.
    discord_bot_token: str = field(default="", repr=False)
    discord_guild_id: str = ""                  # optional: commands appear instantly here
    alert_cooldown_seconds: float = 900.0

    poll_interval_seconds: float = 10.0
    request_timeout_seconds: float = 15.0
    watch: list[WatchEntry] = field(default_factory=list)
    # Defaults for the limits; /limits in Discord overrides them at runtime.
    max_claim_price: Decimal | None = None      # most per unit on any buy, None = off
    max_daily_spend: Decimal | None = None      # per UTC day, both sites together
    max_claim_quantity: int = 0                 # units per buy, 0 = all the balance allows
    # Only an UNCLEAR buy pauses its site: until the site shows what happened, or
    # this many minutes pass. 0 = until `mm2claimer resume`.
    claim_hold_timeout_minutes: float = 30.0
    claim_status_interval_seconds: float = 20.0
    act_on_first_sight: bool = False            # buy items already listed at startup
    balance_refresh_seconds: float = 30.0       # background balance read per site
    # A poll searches the sites for each watched name while the watchlist is this
    # short; with more names it reads the whole market instead.
    search_query_limit: int = 6
    # BloxyMarket rate-limits by IP over a sustained window. sniper_bot ran for months at
    # ~38 requests/min (bursts of 9 at 0.4 s) and got blocked at ~78/min.
    bloxymarket_requests_per_minute: float = 36.0

    # Sessions, read from env only. Never logged.
    bhopp_token: str = field(default="", repr=False)
    bloxymarket_cookie: str = field(default="", repr=False)
    config_file: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mm2claimer.sqlite3"

    @property
    def status_file(self) -> Path:
        return self.data_dir / "status.json"

    def watch_for(self, name: str, slug: str, market: str) -> WatchEntry | None:
        return next((w for w in self.watch if w.matches(name, slug, market)), None)

    def redacted(self) -> dict:
        return {
            "mode": self.mode,
            "markets": sorted(self.markets),
            "discord_webhook": "set" if self.discord_webhook_url else "not set",
            "discord_bot": "set" if self.discord_bot_token else "not set",
            "poll_interval_seconds": self.poll_interval_seconds,
            "watch": [{"item": w.item, "market": w.market,
                       "max_price": str(w.max_price) if w.max_price else None,
                       "min_qty": w.min_qty, "max_qty": w.max_qty}
                      for w in self.watch],
            "max_claim_price": str(self.max_claim_price) if self.max_claim_price else None,
            "max_daily_spend": str(self.max_daily_spend) if self.max_daily_spend else None,
            "max_claim_quantity": self.max_claim_quantity,
            "claim_hold_timeout_minutes": self.claim_hold_timeout_minutes,
            "bhopp_token": "set" if self.bhopp_token else "not set",
            "bloxymarket_cookie": "set" if self.bloxymarket_cookie else "not set",
        }


def _load_dotenv():
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    if Path(".env").exists():
        load_dotenv(".env", override=False)


def load_settings(config_file: str | os.PathLike | None = None,
                  env: dict[str, str] | None = None) -> Settings:
    """Build validated Settings. Raises ConfigError listing every problem.

    Passing `env` explicitly (as tests do) skips .env and the ./config.toml lookup.
    """
    use_defaults = env is None
    if env is None:
        _load_dotenv()
        env = dict(os.environ)
    problems: list[str] = []

    def dec(value, name, *, allow_zero_as_off=True) -> Decimal | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        try:
            d = Decimal(str(value).strip())
        except InvalidOperation:
            problems.append(f"{name} must be a number (got {value!r})")
            return None
        if not d.is_finite() or d < 0:
            problems.append(f"{name} must be a non-negative number (got {value!r})")
            return None
        return None if (d == 0 and allow_zero_as_off) else d

    def flt(value, name, default, minimum=0.0) -> float:
        if value is None or (isinstance(value, str) and not value.strip()):
            return default
        try:
            f = float(str(value).strip())
        except ValueError:
            problems.append(f"{name} must be a number (got {value!r})")
            return default
        if f != f or f < minimum:
            problems.append(f"{name} must be >= {minimum:g} (got {value!r})")
            return default
        return f

    def whole(value, name, default, minimum=0) -> int:
        if value is None or (isinstance(value, str) and not value.strip()):
            return default
        try:
            n = int(str(value).strip())
            if n < minimum:
                raise ValueError
            return n
        except ValueError:
            problems.append(f"{name} must be a whole number, {minimum} or more")
            return default

    path = config_file or env.get("CONFIG_FILE") or None
    raw: dict = {}
    if not path and use_defaults and Path("config.toml").exists():
        path = "config.toml"
    if path:
        p = Path(path)
        if not p.exists():
            raise ConfigError([f"config file not found: {p}"])
        try:
            raw = tomllib.loads(p.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise ConfigError([f"{p} is not valid TOML: {e}"]) from e

    def pick(env_key: str, key: str):
        v = env.get(env_key, "")
        return v if v.strip() else raw.get(key)

    s = Settings(config_file=Path(path) if path else None)

    mode = str(pick("MODE", "mode") or "monitor").strip().lower()
    if mode not in MODES:
        problems.append(f"MODE must be one of: {', '.join(MODES)} (got {mode!r})")
        mode = "monitor"
    s.mode = mode
    markets = parse_markets(str(pick("MARKETS", "markets") or ""))
    if markets is None:
        problems.append("MARKETS must be bhopp, bloxymarket or both")
        markets = set(MARKETS)
    s.markets = markets
    s.data_dir = Path(pick("DATA_DIR", "data_dir") or "data")
    s.log_dir = Path(pick("LOG_DIR", "log_dir") or "logs")
    level = str(pick("LOG_LEVEL", "log_level") or "INFO").strip().upper()
    if level not in LOG_LEVELS:
        problems.append(f"LOG_LEVEL must be one of: {', '.join(LOG_LEVELS)}")
        level = "INFO"
    s.log_level = level
    s.log_json = str(pick("LOG_JSON", "log_json") or "true").strip().lower() \
        not in ("0", "false", "no", "off")

    for secret_key in ("discord_webhook_url", "discord_bot_token", "bhopp_token",
                       "bloxymarket_cookie"):
        if raw.get(secret_key):
            problems.append(f"put {secret_key.upper()} in the environment (.env), "
                            "not in the config file")
    url = env.get("DISCORD_WEBHOOK_URL", "").strip()
    if url and not WEBHOOK_RE.match(url):
        problems.append("DISCORD_WEBHOOK_URL does not look like a Discord webhook URL "
                        "(https://discord.com/api/webhooks/<id>/<token>)")
        url = ""
    s.discord_webhook_url = url
    register_secret(url)
    s.discord_username = str(raw.get("discord_username") or s.discord_username)[:80]
    if "discord" in s.discord_username.lower():
        problems.append("discord_username must not contain \"discord\" (Discord rejects "
                        "every message from such a webhook name)")
    mention = str(pick("DISCORD_MENTION_USER_ID", "discord_mention_user_id") or "").strip()
    if mention and not mention.isdigit():
        problems.append("DISCORD_MENTION_USER_ID must be a numeric Discord user ID")
    elif mention:
        s.discord_mention = f"<@{mention}>"
        s.discord_owner_id = mention
    s.discord_bot_token = env.get("DISCORD_BOT_TOKEN", "").strip()
    if s.discord_bot_token:
        register_secret(s.discord_bot_token)
        if not s.discord_owner_id:
            problems.append("DISCORD_BOT_TOKEN needs DISCORD_MENTION_USER_ID: it is the only "
                            "Discord user allowed to use the commands")
    guild = str(pick("DISCORD_GUILD_ID", "discord_guild_id") or "").strip()
    if guild and not guild.isdigit():
        problems.append("DISCORD_GUILD_ID must be a numeric server ID")
    s.discord_guild_id = guild
    s.alert_cooldown_seconds = flt(raw.get("alert_cooldown_seconds"),
                                   "alert_cooldown_seconds", 900.0)

    s.poll_interval_seconds = flt(pick("POLL_INTERVAL_SECONDS", "poll_interval_seconds"),
                                  "POLL_INTERVAL_SECONDS", 10.0, minimum=MIN_POLL_INTERVAL)
    s.request_timeout_seconds = flt(raw.get("request_timeout_seconds"),
                                    "request_timeout_seconds", 15.0, minimum=1.0)
    s.max_claim_price = dec(pick("MAX_CLAIM_PRICE", "max_claim_price"), "MAX_CLAIM_PRICE")
    s.max_daily_spend = dec(pick("MAX_DAILY_SPEND", "max_daily_spend"), "MAX_DAILY_SPEND")
    s.max_claim_quantity = whole(pick("MAX_CLAIM_QUANTITY", "max_claim_quantity"),
                                 "MAX_CLAIM_QUANTITY", 0)
    s.claim_hold_timeout_minutes = flt(
        pick("CLAIM_HOLD_TIMEOUT_MINUTES", "claim_hold_timeout_minutes"),
        "CLAIM_HOLD_TIMEOUT_MINUTES", 30.0)
    s.claim_status_interval_seconds = flt(raw.get("claim_status_interval_seconds"),
                                          "claim_status_interval_seconds", 20.0,
                                          minimum=1.0)
    s.act_on_first_sight = str(pick("ACT_ON_FIRST_SIGHT", "act_on_first_sight") or "false"
                               ).strip().lower() in ("1", "true", "yes", "on")
    s.balance_refresh_seconds = flt(pick("BALANCE_REFRESH_SECONDS", "balance_refresh_seconds"),
                                    "BALANCE_REFRESH_SECONDS", 30.0, minimum=5.0)
    s.search_query_limit = whole(pick("SEARCH_QUERY_LIMIT", "search_query_limit"),
                                 "SEARCH_QUERY_LIMIT", 6)
    s.bloxymarket_requests_per_minute = flt(
        pick("BLOXYMARKET_REQUESTS_PER_MINUTE", "bloxymarket_requests_per_minute"),
        "BLOXYMARKET_REQUESTS_PER_MINUTE", 36.0, minimum=1.0)

    def limit(value, name) -> Decimal | None:
        """Per-item price limit. 0 is a real number here, not "off": a max_price of 0
        would never buy, so it is refused instead of silently meaning no limit."""
        d = dec(value, name, allow_zero_as_off=False)
        if d is not None and d == 0:
            problems.append(f"{name} is 0, which never matches; remove it for no limit")
            return None
        return d

    seen_ids: set[str] = set()

    def add(entry: WatchEntry, where: str):
        if entry.watch_id in seen_ids:
            problems.append(f"{where} {entry.item!r} ({entry.market}) is listed twice")
            return
        seen_ids.add(entry.watch_id)
        s.watch.append(entry)

    watch_raw = raw.get("watch", [])
    if not isinstance(watch_raw, list):
        problems.append("[[watch]] entries must be an array of tables")
        watch_raw = []
    for i, w in enumerate(watch_raw):
        if not isinstance(w, dict) or not str(w.get("item") or "").strip():
            problems.append(f"watch entry #{i + 1} needs an item name")
            continue
        item = str(w["item"]).strip()
        if w.get("side") not in (None, "withdraw", "buy"):
            problems.append(f"watch entry {item!r}: these sites only sell items, there is "
                            "no deposit side")
            continue
        market = parse_watch_market(str(w.get("market") or "both"))
        if market is None:
            problems.append(f"watch entry {item!r}: market must be one of "
                            f"{', '.join(WATCH_MARKETS)}")
            continue
        try:
            min_qty = int(w.get("min_quantity") or 1)
            max_qty = int(w["max_quantity"]) if w.get("max_quantity") else None
            if min_qty < 1 or (max_qty is not None and max_qty < min_qty):
                raise ValueError
        except (TypeError, ValueError):
            problems.append(f"watch entry {item!r}: min_quantity/max_quantity must be whole "
                            "numbers, 1+, with max at least min")
            continue
        add(WatchEntry(item, market, limit(w.get("max_price"), f"max_price for {item!r}"),
                       norm_name(item), min_qty, max_qty), "watch entry")
    # Quick env-only watchlist, handy on Railway:
    #   WATCH="Harvester<=3.5, Chroma Seer@bloxymarket<=40 x1-2"
    for part in [p.strip() for p in env.get("WATCH", "").split(",") if p.strip()]:
        m = WATCH_ENTRY_RE.match(part)
        if not m:
            problems.append(f"WATCH entry {part!r}: use Item, Item<=max, Item@bhopp<=max "
                            "or Item<=max x2-5")
            continue
        item = m["item"].strip()
        market = parse_watch_market(m["market"] or "both")
        if market is None:
            problems.append(f"WATCH entry {part!r}: market must be one of "
                            f"{', '.join(WATCH_MARKETS)}")
            continue
        mn_q = max(1, int(m["qty"] or 1))
        mx_q = int(m["maxqty"]) if m["maxqty"] else None
        if mx_q is not None and mx_q < mn_q:
            problems.append(f"WATCH entry {part!r}: x{mn_q}-{mx_q} has max below min")
            continue
        if norm_name(item):
            add(WatchEntry(item, market, limit(m["max"], f"WATCH max price for {item!r}")
                           if m["max"] else None, norm_name(item), mn_q, mx_q), "WATCH entry")
    if not s.watch and not s.discord_bot_token:
        problems.append("the watchlist is empty: add [[watch]] entries to config.toml, "
                        "set WATCH in .env, or set DISCORD_BOT_TOKEN and use /watch add")

    s.bhopp_token = env.get("BHOPP_TOKEN", "").strip()
    if s.bhopp_token.lower().startswith("bearer "):
        s.bhopp_token = s.bhopp_token[7:].strip()
    s.bloxymarket_cookie = env.get("BLOXYMARKET_COOKIE", "").strip()
    if s.bloxymarket_cookie.lower().startswith("cookie:"):
        s.bloxymarket_cookie = s.bloxymarket_cookie[7:].strip()
    register_secret(s.bhopp_token)
    register_secret(s.bloxymarket_cookie)
    if s.mode == "claim":
        if not s.discord_webhook_url:
            problems.append("claim mode needs DISCORD_WEBHOOK_URL, otherwise nobody hears "
                            "about what was bought")
        if not (s.bhopp_token or s.bloxymarket_cookie):
            problems.append("claim mode needs BHOPP_TOKEN or BLOXYMARKET_COOKIE in .env (see "
                            "README: \"Getting your sessions\")")
        # Claim mode spends real money: every entry needs a price limit.
        for w in s.watch:
            problems.extend(limit_problems(w, s.max_claim_price))

    if problems:
        raise ConfigError(problems)
    return s
