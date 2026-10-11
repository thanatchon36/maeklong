"""Run the universe screener once and print the ranked CANDIDATE list (M2 acceptance).

Public endpoints only: exchangeInfo, ticker/24hr, bookTicker (+ recorded history in data_dir).

    uv run python tools/screen_once.py [--config config/config.yaml] [--all] [--json out.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict

from riverbank.core.config import load_config
from riverbank.core.models import Mode
from riverbank.exchange.environments import ENVIRONMENTS
from riverbank.exchange.rest import RestClient
from riverbank.universe.screener import Screener


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument("--demo", action="store_true", help="screen demo-fapi market data instead of production")
    ap.add_argument("--all", action="store_true", help="also list excluded symbols with reasons")
    ap.add_argument("--json", help="write all rows to this JSON file")
    args = ap.parse_args()
    cfg = load_config(args.config)
    env = ENVIRONMENTS[Mode.DEMO if args.demo else Mode.SHADOW]
    rest = RestClient(env.rest)
    try:
        rows = await Screener(cfg, rest).screen_once()
    finally:
        await rest.close()
    elig = [r for r in rows if r.eligible]
    print(f"screened {len(rows)} symbols: {len(elig)} eligible, "
          f"{sum(r.candidate for r in rows)} CANDIDATE (max {cfg.universe.max_candidate_symbols}); "
          f"round-trip maker fee {2 * cfg.account.fee_maker_bps:.1f} bps")
    hdr = (f"{'rank':>4} {'symbol':<14}{'state':<10}{'score':>8}{'spread':>8}{'tick':>7}{'vol$M':>8}"
           f"{'range':>8}{'toxic':>7}{'fills/m':>8}{'edge×':>7}{'qty':>10}{'notnl':>7}{'units':>6} src")
    print(hdr)
    for r in elig:
        print(f"{r.rank:>4} {r.symbol:<14}{'CANDIDATE' if r.candidate else '-':<10}{r.score:>8.2f}"
              f"{r.spread_bps:>8.2f}{r.tick_bps:>7.2f}{r.quote_vol_musd:>8.1f}{r.range_bps:>8.0f}"
              f"{r.toxicity_proxy:>7.0f}{r.fill_opportunity:>8.1f}{r.edge_ratio:>7.2f}{r.order_qty:>10}"
              f"{r.order_notional:>7.1f}{r.inventory_units:>6} {r.source}")
    if args.all:
        print("\nexcluded:")
        for r in rows:
            if not r.eligible:
                print(f"  {r.symbol:<16} {', '.join(r.reasons)}")
    else:
        for sym in cfg.universe.hedge_only:
            r = next((x for x in rows if x.symbol == sym), None)
            if r is not None:
                print(f"  {sym}: excluded ({', '.join(r.reasons)})")
    if args.json:
        with open(args.json, "w") as f:
            json.dump([asdict(r) for r in rows], f, indent=1, default=str)


if __name__ == "__main__":
    asyncio.run(main())
