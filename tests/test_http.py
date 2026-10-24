import asyncio
import time

import pytest
from aiohttp import web

from conftest import run
from mm2claimer.http import (AuthError, BadResponse, HttpClient, RateLimited, TransientError,
                             parse_retry_after)


async def _with_server(fn, **client_kw):
    app = web.Application()

    async def ok(req):
        return web.json_response({"q": dict(req.query)},
                                 headers={"Set-Cookie": "session=rot; Path=/"})

    async def limited(req):
        return web.Response(status=429, headers={"Retry-After": "42"})

    async def bare_403(req):
        return web.Response(status=403, text="<html>blocked</html>")

    async def app_403(req):
        return web.json_response({"errors": ["Your account is banned."],
                                  "code": "account_banned"}, status=403)

    async def bhopp_400(req):
        return web.json_response({"errors": ["Not enough balance."],
                                  "code": "insufficient_balance"}, status=400)

    async def bloxy_400(req):
        return web.json_response({"message": "Item is no longer available"}, status=400)

    async def unauth(req):
        return web.Response(status=401)

    async def boom(req):
        return web.Response(status=502)

    async def html(req):
        return web.Response(text="<html>nope</html>")

    async def redirect(req):
        return web.Response(status=302, headers={"Location": "https://elsewhere.test/"})

    async def slow(req):
        await asyncio.sleep(2)
        return web.json_response({})

    for path, h in [("/ok", ok), ("/429", limited), ("/403", bare_403), ("/403j", app_403),
                    ("/400b", bhopp_400), ("/400x", bloxy_400), ("/401", unauth),
                    ("/502", boom), ("/html", html), ("/302", redirect), ("/slow", slow)]:
        app.router.add_route("*", path, h)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    client = HttpClient(**{"min_host_interval": 0, "default_timeout": 0.5, **client_kw})
    try:
        await fn(client, f"http://127.0.0.1:{port}")
    finally:
        await client.close()
        await runner.cleanup()


def test_ok_params_and_cookies():
    async def t(c, base):
        r = await c.request_json("GET", base + "/ok", params={"search": "seer", "page": 1})
        assert r.data == {"q": {"search": "seer", "page": "1"}}
        assert r.cookies == {"session": "rot"}
    run(_with_server(t))


@pytest.mark.parametrize("path,exc", [("/429", RateLimited), ("/403", RateLimited),
                                      ("/401", AuthError), ("/502", TransientError),
                                      ("/html", BadResponse), ("/302", BadResponse),
                                      ("/slow", TransientError)])
def test_error_mapping(path, exc):
    async def t(c, base):
        with pytest.raises(exc):
            await c.request_json("GET", base + path)
    run(_with_server(t))


def test_site_refusals_carry_their_words_and_code():
    async def t(c, base):
        with pytest.raises(BadResponse) as e:
            await c.request_json("POST", base + "/400b", json_body={})
        assert "Not enough balance" in str(e.value) and e.value.code == "insufficient_balance"
        with pytest.raises(BadResponse) as e:
            await c.request_json("POST", base + "/403j", json_body={})
        assert e.value.status == 403 and "banned" in str(e.value)
        with pytest.raises(BadResponse) as e:
            await c.request_json("POST", base + "/400x", json_body={})
        assert "no longer available" in str(e.value)
    run(_with_server(t))


def test_retry_after_read():
    async def t(c, base):
        with pytest.raises(RateLimited) as e:
            await c.request_json("GET", base + "/429")
        assert e.value.retry_after == 42
    run(_with_server(t))
    assert parse_retry_after(None) == 60 and parse_retry_after("5000") == 900


def test_per_host_gap():
    async def t(c, base):
        t0 = time.monotonic()
        for _ in range(3):
            await c.request_json("GET", base + "/ok")
        assert time.monotonic() - t0 >= 0.4
    run(_with_server(t, host_intervals={"127.0.0.1": 0.2}))


def test_budget_allows_a_burst_then_paces():
    from mm2claimer.http import HostLimit

    async def t(c, base):
        t0 = time.monotonic()
        for _ in range(3):                       # the burst: only min_gap apart
            await c.request_json("GET", base + "/ok")
        burst = time.monotonic() - t0
        await c.request_json("GET", base + "/ok")  # budget used up: waits for a refill
        paced = time.monotonic() - t0
        assert 0.08 <= burst < 0.5, burst
        # 120/min refills one every 0.5 s: the 4th waits for the part not refilled yet.
        assert paced - burst >= 0.3, (burst, paced)
    run(_with_server(t, host_limits={"127.0.0.1": HostLimit(120, burst=3, min_gap=0.05)}))


def test_unspaced_request_skips_the_budget_wait_but_counts():
    from mm2claimer.http import HostLimit

    async def t(c, base):
        await c.request_json("GET", base + "/ok")          # uses the only token
        t0 = time.monotonic()
        await c.request_json("GET", base + "/ok", spaced=False)
        assert time.monotonic() - t0 < 0.3                  # did not wait
        t0 = time.monotonic()
        await c.request_json("GET", base + "/ok")           # repays both: waits ~1 s
        assert time.monotonic() - t0 >= 0.8
    run(_with_server(t, host_limits={"127.0.0.1": HostLimit(60, burst=1, min_gap=0.0)}))
