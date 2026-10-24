"""Discord webhook notifications.

Messages go through a queue drained by one background task, so a slow or
failing Discord never blocks polling or buying. The notifier:
* drops repeats of the same key within a cooldown (no alert spam);
* batches up to 10 embeds per webhook message (Discord's limit);
* honours 429 retry_after and retries 5xx a few times, then gives up and logs;
* scrubs anything that looks like a token or webhook URL from text it sends.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import unquote

import aiohttp

from . import USER_AGENT
from .models import ClaimOutcome, Listing

log = logging.getLogger(__name__)

GREEN, BLUE, ORANGE, RED, GREY = 0x2ECC71, 0x3498DB, 0xE67E22, 0xE74C3C, 0x95A5A6
MAX_EMBEDS = 10
LABELS = {"bhopp": "BHOPP", "bloxymarket": "BloxyMarket"}
_SECRET_PATTERNS = [
    re.compile(r"https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\S+"),
    re.compile(r"(?i)(bearer\s+)[\w\-.~+/=]+"),
    re.compile(r"(?i)((?:token|cookie|authorization|session|secret|password)\"?\s*[:=]\s*\"?)"
               r"[^\s\",;]+"),
    re.compile(r"eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}"),   # JWTs (the BHOPP token)
]
# Exact secret values registered at runtime (the configured tokens, every cookie value
# including rotated ones). Scrubbed wherever they appear, whatever the format.
_KNOWN_SECRETS: set[str] = set()


def label(market: str) -> str:
    return LABELS.get(market, market.capitalize())


def register_secret(value) -> None:
    v = str(value or "").strip()
    if len(v) >= 8:
        _KNOWN_SECRETS.add(v)
        decoded = unquote(v)
        if decoded != v and len(decoded) >= 8:
            _KNOWN_SECRETS.add(decoded)


def redact(text: str) -> str:
    out = str(text)
    for secret in sorted(_KNOWN_SECRETS, key=len, reverse=True):
        if secret in out:
            out = out.replace(secret, "[redacted]")
    for pat in _SECRET_PATTERNS:
        out = pat.sub(lambda m: (m.group(1) if m.groups() else "") + "[redacted]", out)
    return out


_MD_SPECIAL = re.compile(r"([\\*_~`|\[\]()<>#])")


def _md(text) -> str:
    """Escape Discord markdown, so a name from the site can't inject links or formatting."""
    return _MD_SPECIAL.sub(r"\\\1", str(text))


def _ts(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def _clip(s: str, n: int) -> str:
    s = redact(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def _fields(*pairs: tuple[str, str, bool]) -> list[dict]:
    return [{"name": _clip(k, 256), "value": _clip(v or "—", 1024), "inline": inline}
            for k, v, inline in pairs if v is not None]


def _https(url: str) -> str | None:
    # Discord rejects (HTTP 400) an embed url that is not http(s), which would drop
    # the alert. Anything else stays out of link positions.
    return url if url.startswith("https://") else None


def money(value, unit: str) -> str:
    return f"${value}" if unit == "USD" else f"{value} {unit}"


# ------------------------------------------------------------------ embeds


def claim_embed(listing: Listing, outcome: ClaimOutcome) -> dict:
    """The one that matters: "bought 3x Harvester on BHOPP, here is what happens next"."""
    qty = outcome.quantity or max(1, listing.quantity)
    who = _md(outcome.username or "your account")
    desc = f"Bought on **{label(listing.marketplace)}** as **{who}**."
    if outcome.note:
        desc += "\n" + _md(outcome.note)
    link = _https(outcome.link)
    if link:
        desc += f"\n**[Open it on {label(listing.marketplace)}]({link})**"
    total = outcome.charged if outcome.charged is not None else listing.price * qty
    e = {
        "title": _clip(f"✅ {qty}× {listing.name} bought", 256),
        "description": _clip(desc, 4096),
        "url": _https(listing.url),
        "color": GREEN,
        "fields": _fields(
            ("Item", _md(listing.name), True),
            ("Quantity", str(qty), True),
            ("Price each", money(listing.price, listing.unit), True),
            ("Total", money(total, listing.unit), True),
            ("Seller", _md(outcome.seller) if outcome.seller else None, True),
            ("Account", _md(outcome.username) if outcome.username else None, True),
            ("Order", f"`{outcome.claim_id}`" if outcome.claim_id else None, True),
            ("Rarity", listing.rarity or None, True),
        ),
        "timestamp": _ts(listing.detected_at),
        "footer": {"text": f"{label(listing.marketplace)} · {listing.listing_id}"},
    }
    if listing.image.startswith("https://"):
        e["thumbnail"] = {"url": listing.image}
    return e


def available_embed(listing: Listing, mode: str, reason: str = "") -> dict:
    e = {
        "title": _clip(f"👀 {listing.name} on {label(listing.marketplace)}", 256),
        "description": (reason or ("Monitor mode: not buying." if mode == "monitor"
                                   else "Matched your watchlist.")),
        "url": _https(listing.url),
        "color": BLUE,
        "fields": _fields(
            ("Item", _md(listing.name), True),
            ("Price", money(listing.price, listing.unit), True),
            ("At this price", str(listing.quantity), True),
            ("Seller", _md(listing.extra.get("seller")) if listing.extra.get("seller")
             else None, True),
            ("Link", _https(listing.url), False),
        ),
        "timestamp": _ts(listing.detected_at),
        "footer": {"text": label(listing.marketplace)},
    }
    if listing.image.startswith("https://"):
        e["thumbnail"] = {"url": listing.image}
    return e


def claim_failed_embed(listing: Listing, outcome: ClaimOutcome) -> dict:
    unknown = outcome.status.value == "unknown"
    site = label(listing.marketplace)
    return {
        "title": _clip(("⚠️ Buy outcome unknown: " if unknown else "❌ Buy failed: ")
                       + f"{listing.name} on {site}", 256),
        "description": _clip(
            (outcome.detail or "no detail")
            + (f"\nCheck {site} manually. Buying there stays paused until the bot can see "
               "whether this went through." if unknown else ""), 4096),
        "url": _https(listing.url),
        "color": ORANGE if unknown else RED,
        "fields": _fields(("Status", outcome.status.value, True),
                          ("Price", money(listing.price, listing.unit), True),
                          ("Quantity", str(max(1, listing.quantity)), True)),
        "timestamp": _ts(),
    }


def system_embed(title: str, message: str, level: str = "info") -> dict:
    color = {"info": GREY, "ok": GREEN, "warn": ORANGE, "error": RED}.get(level, GREY)
    return {"title": _clip(title, 256), "description": _clip(message, 4096), "color": color,
            "timestamp": _ts(), "footer": {"text": "mm2-claimer"}}


# ------------------------------------------------------------------ sender


@dataclass
class _Msg:
    embed: dict
    content: str = ""
    key: str | None = None
    created: float = field(default_factory=time.monotonic)


class DiscordNotifier:
    def __init__(self, webhook_url: str, *, username: str = "MM2 Claimer",
                 cooldown_seconds: float = 900.0, mention: str = "",
                 session_factory=None, max_retries: int = 4):
        self.url = webhook_url
        self.username = username
        self.cooldown = cooldown_seconds
        self.mention = mention
        self.max_retries = max_retries
        self._session_factory = session_factory or (
            lambda: aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}))
        self._session = None
        self._queue: asyncio.Queue[_Msg] = asyncio.Queue(maxsize=500)
        self._last_sent: dict[str, float] = {}
        self._task: asyncio.Task | None = None
        self.sent = 0
        self.failed = 0
        self.dropped = 0

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def start(self):
        if self.enabled and self._task is None:
            self._task = asyncio.create_task(self._run(), name="discord-notifier")

    async def stop(self, flush_timeout: float = 10.0):
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._queue.join(), flush_timeout)
        except asyncio.TimeoutError:
            log.warning("discord: %d messages unsent at shutdown", self._queue.qsize())
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None
        if self._session is not None:
            await self._session.close()

    def post(self, embed: dict, *, key: str | None = None, ping: bool = False) -> bool:
        """Queue an embed. Returns False if suppressed (cooldown/disabled/full)."""
        if not self.enabled:
            return False
        now = time.monotonic()
        if key is not None:
            last = self._last_sent.get(key)
            if last is not None and now - last < self.cooldown:
                return False
            self._last_sent[key] = now
            if len(self._last_sent) > 5000:  # bound memory
                cutoff = now - self.cooldown
                self._last_sent = {k: v for k, v in self._last_sent.items() if v >= cutoff}
        try:
            self._queue.put_nowait(_Msg(embed, self.mention if ping else "", key))
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            log.warning("discord queue full, dropping %s", embed.get("title"))
            return False

    # ---- convenience
    def claim_succeeded(self, listing: Listing, outcome: ClaimOutcome):
        self.post(claim_embed(listing, outcome), ping=True)

    def claim_failed(self, listing: Listing, outcome: ClaimOutcome):
        self.post(claim_failed_embed(listing, outcome),
                  key=f"claimfail:{listing.marketplace}:{listing.listing_id}:"
                      f"{outcome.status.value}",
                  ping=outcome.status.value == "unknown")

    def available(self, listing: Listing, mode: str, reason: str = ""):
        self.post(available_embed(listing, mode, reason),
                  key=f"avail:{listing.marketplace}:{listing.listing_id}:{listing.price}",
                  ping=True)

    def system(self, title: str, message: str, level: str = "info", key: str | None = None):
        self.post(system_embed(title, message, level), key=key)

    # ---- worker

    async def _run(self):
        while True:
            first = await self._queue.get()
            batch = [first]
            # Grab whatever else is already waiting, up to Discord's embed limit.
            while len(batch) < MAX_EMBEDS and not self._queue.empty():
                batch.append(self._queue.get_nowait())
            try:
                await self._deliver(batch)
            except Exception:  # never let the worker die
                log.exception("discord: unexpected delivery error")
                self.failed += len(batch)
            finally:
                for _ in batch:
                    self._queue.task_done()

    async def _deliver(self, batch: list[_Msg]):
        content = " ".join(dict.fromkeys(m.content for m in batch if m.content))
        # Only the configured user can ever be pinged (no roles, no @everyone).
        users = re.findall(r"\d+", self.mention or "")
        payload = {"username": self.username, "embeds": [m.embed for m in batch],
                   "allowed_mentions": {"parse": [], "users": users}}
        if content:
            payload["content"] = content[:2000]
        if self._session is None or getattr(self._session, "closed", False):
            self._session = self._session_factory()
        for attempt in range(self.max_retries + 1):
            try:
                async with self._session.post(self.url, json=payload,
                                              timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status in (200, 204):
                        self.sent += len(batch)
                        return
                    if r.status == 429:
                        try:
                            body = await r.json(content_type=None)
                            wait = float(body.get("retry_after", 1.0))
                        except Exception:
                            wait = float(r.headers.get("Retry-After", 1.0) or 1.0)
                        log.info("discord rate limited, waiting %.1fs", wait)
                        await asyncio.sleep(min(max(wait, 0.5), 60.0))
                        continue
                    if r.status >= 500:
                        await asyncio.sleep(min(2 ** attempt, 30))
                        continue
                    text = await r.text()
                    if len(batch) > 1:
                        # One bad embed must not drop the rest: send them one by one.
                        log.warning("discord rejected a batch (HTTP %s), resending singly",
                                    r.status)
                        for m in batch:
                            await self._deliver([m])
                        return
                    log.error("discord rejected webhook message: HTTP %s %s",
                              r.status, redact(text)[:200])
                    self.failed += len(batch)
                    return
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("discord delivery failed (%s), attempt %d",
                            type(e).__name__, attempt + 1)
                await asyncio.sleep(min(2 ** attempt, 30))
        self.failed += len(batch)
        log.error("discord: giving up on %d message(s) after retries", len(batch))
