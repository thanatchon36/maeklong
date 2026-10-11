"""Print SymbolSpecs loaded from the live public exchangeInfo (M0 acceptance).

Public endpoint only (GET /fapi/v1/exchangeInfo) — no keys needed or used.

    uv run python tools/show_specs.py [SYMBOL ...] [--demo]
"""

from __future__ import annotations

import argparse
import asyncio

from riverbank.exchange.environments import DEMO_REST, PROD_REST
from riverbank.exchange.rest import RestClient
from riverbank.exchange.specs import fetch_exchange_info

DEFAULT = ["DOGEUSDT", "XRPUSDT", "SOLUSDT", "ADAUSDT", "LINKUSDT"]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*", default=DEFAULT)
    ap.add_argument("--demo", action="store_true", help="use demo-fapi instead of production")
    args = ap.parse_args()
    rest = RestClient(DEMO_REST if args.demo else PROD_REST)
    try:
        info = await fetch_exchange_info(rest)
    finally:
        await rest.close()
    print(f"exchangeInfo: {len(info.specs)} symbols, serverTime={info.server_time}")
    for rl in info.rate_limits:
        print(f"  rateLimit {rl.limit_type:<15} {rl.interval_num}{rl.interval[0]:<3} limit={rl.limit}")
    hdr = f"{'symbol':<14}{'tick':>12}{'step':>10}{'minQty':>10}{'minNotional':>12}{'pxPrec':>7}{'qtyPrec':>8}  status"
    print(hdr)
    for sym in args.symbols:
        s = info.specs.get(sym.upper())
        if s is None:
            print(f"{sym:<14} NOT FOUND")
            continue
        print(f"{s.symbol:<14}{s.tick!s:>12}{s.step!s:>10}{s.min_qty!s:>10}{s.min_notional!s:>12}"
              f"{s.price_precision:>7}{s.qty_precision:>8}  {s.status}/{s.contract_type}")


if __name__ == "__main__":
    asyncio.run(main())
