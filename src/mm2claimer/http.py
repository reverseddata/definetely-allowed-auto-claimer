"""Small async HTTP client shared by marketplace adapters.

It turns HTTP and transport failures into a handful of exception types the
service knows how to react to. It also enforces a minimum gap between requests
to the same host, so one adapter can never burst-request a marketplace.

It does NOT rotate identities, spoof headers, or impersonate a browser.
Requests carry an honest User-Agent naming this project.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from . import USER_AGENT

log = logging.getLogger(__name__)


class MarketplaceError(Exception):
    """Base class for adapter-visible failures."""


class TransientError(MarketplaceError):
    """Timeouts, connection resets, 5xx: retry later with backoff."""


class RequestNotSent(TransientError):
    """The request provably never left this machine (DNS/connect failure).
    The only failure after which a purchase may be retried safely."""


class RateLimited(MarketplaceError):
    def __init__(self, retry_after: float, status: int = 429):
        self.retry_after = retry_after
        self.status = status
        super().__init__(f"HTTP {status}, retry after {retry_after:.0f}s")


class AuthError(MarketplaceError):
    """401: credentials missing or expired."""


class BadResponse(MarketplaceError):
    """Answered, but not with something we can use (4xx, non-JSON, wrong shape).
    `code` is the site's machine-readable reason when it sent one
    (BHOPP: "insufficient_balance")."""

    def __init__(self, msg: str, status: int | None = None, code: str = ""):
        self.status = status
        self.code = code
        super().__init__(msg)


def _error_parts(text: str) -> tuple[str, str]:
    """(the site's own words for a refusal, its error code), short and on one line.
    BHOPP answers {"errors": [...], "code": ...}; BloxyMarket {"message": ...}."""
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        code = data.get("code")
        code = str(code)[:64] if isinstance(code, (str, int)) else ""
        for k in ("errors", "message", "error", "detail", "msg"):
            v = data.get(k)
            if isinstance(v, list):
                v = "; ".join(map(str, v))
            if isinstance(v, (str, int)) and str(v).strip():
                return " ".join(str(v).split())[:200], code
        return "", code
    return " ".join((text or "").split())[:200], ""


def _is_json_error(text: str) -> bool:
    try:
        data = json.loads(text)
    except ValueError:
        return False
    return isinstance(data, dict) and any(data.get(k) for k in
                                          ("errors", "message", "error", "code"))


@dataclass
class HttpResult:
    status: int
    data: Any
    etag: str | None = None
    not_modified: bool = False
    # Cookies the server set on this response (name -> value), e.g. a rotated session.
    cookies: dict[str, str] = field(default_factory=dict)


DEFAULT_RETRY_AFTER = 60.0
MAX_RETRY_AFTER = 900.0


def parse_retry_after(value: str | None, default: float = DEFAULT_RETRY_AFTER) -> float:
    """Retry-After as seconds (delta or HTTP date), clamped to [1, 900]."""
    if not value:
        return default
    try:
        secs = float(value)
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
            secs = dt.timestamp() - time.time()
        except (TypeError, ValueError):
            return default
    return max(1.0, min(MAX_RETRY_AFTER, secs))


class HttpClient:
    def __init__(self, *, min_host_interval: float = 1.0, default_timeout: float = 20.0,
                 user_agent: str = USER_AGENT, host_intervals: dict[str, float] | None = None):
        self.min_host_interval = min_host_interval
        # Per-host overrides: BloxyMarket rate-limits by IP and needs a wider gap.
        self.host_intervals = dict(host_intervals or {})
        self.default_timeout = default_timeout
        self.user_agent = user_agent
        self._session: aiohttp.ClientSession | None = None
        self._host_next: dict[str, float] = {}
        self._host_locks: dict[str, asyncio.Lock] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # DummyCookieJar: cookies are only ever sent when an adapter passes them
            # explicitly. A Set-Cookie from one response is never replayed on others
            # (e.g. the public item polls stay anonymous).
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": self.user_agent, "Accept": "application/json"},
                cookie_jar=aiohttp.DummyCookieJar())
        return self._session

    async def close(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _space(self, host: str):
        """Wait until the host's minimum gap has passed since the last request to it."""
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        gap = self.host_intervals.get(urlsplit(f"//{host}").hostname or host,
                                      self.min_host_interval)
        async with lock:
            now = time.monotonic()
            wait = self._host_next.get(host, 0.0) - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._host_next[host] = time.monotonic() + gap

    async def request_json(self, method: str, url: str, *, json_body: Any = None,
                           params: dict | None = None,
                           headers: dict[str, str] | None = None,
                           timeout: float | None = None,
                           etag: str | None = None, spaced: bool = True) -> HttpResult:
        """`spaced=False` skips the per-host gap: for the one purchase request, which
        must not wait seconds behind the poll that found the item."""
        host = urlsplit(url).netloc
        if spaced:
            await self._space(host)
        hdrs = dict(headers or {})
        if etag:
            hdrs["If-None-Match"] = etag
        session = await self._get_session()
        to = aiohttp.ClientTimeout(total=timeout or self.default_timeout)
        try:
            # Redirects are never followed: a redirect could carry a session cookie or a
            # purchase body to another place. A 3xx is reported as an unclear answer.
            async with session.request(method, url, json=json_body, params=params,
                                       headers=hdrs, timeout=to,
                                       allow_redirects=False) as resp:
                status = resp.status
                resp_etag = resp.headers.get("ETag")
                if status == 304:
                    return HttpResult(status, None, etag or resp_etag, not_modified=True)
                if 300 <= status < 400:
                    raise BadResponse(f"HTTP {status} redirect (not followed)", status)
                if status == 429:
                    raise RateLimited(parse_retry_after(resp.headers.get("Retry-After")),
                                      status)
                if status == 401:
                    raise AuthError("HTTP 401: authentication rejected")
                if status >= 500:
                    raise TransientError(f"HTTP {status}")
                text = await resp.text()
                if status == 403 and not _is_json_error(text):
                    # A bare 403 (a CDN or firewall page) means "blocked", handled like
                    # a rate limit: stop and wait, never push through. A 403 the app
                    # itself explains (a banned account, say) is a refusal, below.
                    raise RateLimited(parse_retry_after(resp.headers.get("Retry-After")),
                                      status)
                if status >= 400:
                    reason, code = _error_parts(text)
                    raise BadResponse(f"HTTP {status}" + (f": {reason}" if reason else ""),
                                      status, code)
                cookies = {k: m.value for k, m in resp.cookies.items()}
        except (aiohttp.ClientConnectorError, aiohttp.ClientConnectorDNSError) as e:
            raise RequestNotSent(f"connect failed: {type(e).__name__}") from e
        except asyncio.TimeoutError as e:
            raise TransientError("timeout") from e
        except aiohttp.ClientError as e:
            raise TransientError(f"transport error: {type(e).__name__}") from e
        try:
            # A full market page is ~200 KB: parse big answers off the event loop.
            data = (await asyncio.to_thread(json.loads, text) if len(text) > 100_000
                    else json.loads(text))
        except ValueError as e:
            raise BadResponse(f"non-JSON response (HTTP {status})", status) from e
        return HttpResult(status, data, resp_etag, cookies=cookies)
