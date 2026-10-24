"""Command line entry point: `mm2claimer <command>` (or `python -m mm2claimer`)."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from . import __version__
from .config import ConfigError, Settings, load_settings
from .logging_setup import setup_logging

log = logging.getLogger("mm2claimer")


def _settings(args) -> Settings:
    try:
        return load_settings(args.config)
    except ConfigError as e:
        print("Configuration problems:", file=sys.stderr)
        for p in e.problems:
            print(f"  - {p}", file=sys.stderr)
        raise SystemExit(2)


def cmd_run(args):
    s = _settings(args)
    setup_logging(s.log_level, s.log_dir, s.log_json)
    from .service import run_service
    try:
        asyncio.run(run_service(s))
    except KeyboardInterrupt:
        pass


def cmd_check(args):
    s = _settings(args)
    print(json.dumps(s.redacted(), indent=2))
    print("configuration OK")


def _fmt_listing(l) -> str:
    seller = l.extra.get("seller")
    return (f"{l.marketplace:<12} {l.name:<28} ${l.price:<8} x{l.quantity:<4}"
            + (f" seller={seller}" if seller else "") + f"  {l.url}")


def cmd_balance(args):
    """Read-only: the account and balance of each configured session."""
    s = _settings(args)
    setup_logging("WARNING")
    from .http import AuthError, MarketplaceError
    from .service import build_adapter, build_http

    async def go():
        http = build_http(s)
        multi = build_adapter(s, http)
        code = 0
        try:
            for name, a in multi.adapters.items():
                if not a.capabilities.claim:
                    print(f"{name}: no session ({a.capabilities.claim_unavailable_reason})")
                    continue
                try:
                    account = await a.read_account()
                    balance = await a.get_balance()
                except AuthError as e:
                    print(f"{name}: session rejected ({e}): paste a fresh one into .env")
                    code = 1
                    continue
                except MarketplaceError as e:
                    print(f"{name}: could not read balance: {e}")
                    code = 1
                    continue
                print(f"{name}: account {account}, balance ${balance}")
                warning = a.session_warning()
                if warning:
                    print(f"  warning: {warning}")
        finally:
            await http.close()
        return code
    raise SystemExit(asyncio.run(go()))


def cmd_probe(args):
    """One live read of both markets for the watched items. Never buys or notifies."""
    s = _settings(args)
    setup_logging("WARNING")
    from .service import build_adapter, build_http

    async def go():
        http = build_http(s)
        multi = build_adapter(s, http)
        try:
            for name, a in multi.adapters.items():
                if name not in s.markets:
                    continue
                keys = {w.key for w in s.watch if w.market in ("both", name)}
                try:
                    listings = await a.get_listings(only=keys) if keys else []
                except Exception as e:  # noqa: BLE001
                    print(f"{name}: poll FAILED: {e}")
                    continue
                print(f"{name}: reachable, buying "
                      + ("supported" if a.capabilities.claim
                         else f"NOT available ({a.capabilities.claim_unavailable_reason})"))
                for w in s.watch:
                    if w.market not in ("both", name):
                        continue
                    hits = [l for l in listings if w.matches(l.name, l.slug, name)]
                    if not hits:
                        print(f"  {w.item:<28} (not listed right now)")
                    for l in hits:
                        print("  " + _fmt_listing(l))
        finally:
            await http.close()
    asyncio.run(go())


def cmd_status(args):
    s = _settings(args)
    if not s.status_file.exists():
        print("no status file yet (has the service run?)")
        raise SystemExit(1)
    print(s.status_file.read_text(encoding="utf-8"))


def cmd_resume(args):
    """Clear the hold an unclear buy put on a market, after checking the site yourself."""
    s = _settings(args)
    from .models import ClaimStatus
    from .store import Store
    store = Store(s.db_path)
    holds = [h for h in store.holds() if args.market in (None, h.market)]
    if not holds:
        print("no unclear buys are holding a market")
    for hold in holds:
        if store.claim(hold.idem_key) is not None:
            store.update_claim(hold.idem_key, ClaimStatus.COMPLETED,
                               detail="resolved by operator")
        store.clear_hold(hold.market)
        print(f"{hold.market}: hold for {hold.name} cleared; the running service resumes "
              f"buying there within {s.claim_status_interval_seconds:g}s")
    store.close()


def cmd_claims(args):
    s = _settings(args)
    from .store import Store
    store = Store(s.db_path)
    rows = store.recent_claims(args.limit)
    if not rows:
        print("no buys recorded")
    for r in rows:
        print(f"{r['created_at'][:19]}  {r['marketplace']:<11} {r['status']:<9} "
              f"{r['quantity']:>3}x {r['name']:<26} ${r['price']:>8}  {r['detail'] or ''}")
    store.close()


def main(argv=None):
    p = argparse.ArgumentParser(prog="mm2claimer",
                                description="BHOPP and BloxyMarket MM2 sniper")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-c", "--config", help="config file (default: ./config.toml or CONFIG_FILE)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the watcher").set_defaults(fn=cmd_run)
    sub.add_parser("check-config", help="validate configuration").set_defaults(fn=cmd_check)
    sub.add_parser("probe", help="one live read of the watched items, no buys"
                   ).set_defaults(fn=cmd_probe)
    sub.add_parser("balance", help="read-only: accounts and balances"
                   ).set_defaults(fn=cmd_balance)
    sub.add_parser("status", help="print the last status snapshot").set_defaults(fn=cmd_status)
    r = sub.add_parser("resume", help="clear an unclear buy's hold after checking the site")
    r.add_argument("market", nargs="?", choices=("bhopp", "bloxymarket"))
    r.set_defaults(fn=cmd_resume)
    c = sub.add_parser("claims", help="list recent buys")
    c.add_argument("--limit", type=int, default=20)
    c.set_defaults(fn=cmd_claims)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
