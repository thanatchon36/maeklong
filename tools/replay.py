"""Replay recorded Parquet data through quoting + paper simulator (deterministic).

    uv run python tools/replay.py --data data --symbols DOGEUSDT XRPUSDT \
        [--start 2026-10-12T00:00] [--end 2026-10-12T06:00] [--out reports/replay] [--check-determinism]

exchangeInfo specs: `--specs file.json` (saved by the shadow run under data/meta/), else fetched
from the public API and saved next to the output for reproducibility.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
from pathlib import Path

from riverbank.analytics.pnl import total
from riverbank.analytics.report import build_daily_report
from riverbank.core.config import load_config
from riverbank.exchange.environments import PROD_REST
from riverbank.exchange.rest import RestClient
from riverbank.exchange.specs import parse_exchange_info
from riverbank.ops.logging import setup_logging
from riverbank.sim.replay import load_events, run_replay
from riverbank.storage.sqlite import SqliteRepository


def _ms(s: str | None) -> int | None:
    if not s:
        return None
    return int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.UTC).timestamp() * 1000)


async def _fetch_specs(path: Path) -> dict:
    rest = RestClient(PROD_REST)
    try:
        data = await rest.public("GET", "/fapi/v1/exchangeInfo")
    finally:
        await rest.close()
    path.write_text(json.dumps(data))
    return data


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument("--data", default="data")
    ap.add_argument("--symbols", nargs="+", required=True)
    ap.add_argument("--hedge", nargs="*", default=[])
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--specs")
    ap.add_argument("--out", default="reports/replay")
    ap.add_argument("--check-determinism", action="store_true")
    ap.add_argument("--log-level", default="WARNING")
    args = ap.parse_args()
    setup_logging(args.log_level)
    cfg = load_config(args.config)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    specs_path = Path(args.specs) if args.specs else out / "exchange_info.json"
    data = json.loads(specs_path.read_text()) if specs_path.exists() else asyncio.run(_fetch_specs(specs_path))
    specs = parse_exchange_info(data).specs
    symbols = [s.upper() for s in args.symbols]
    events = load_events(args.data, symbols + [h.upper() for h in args.hedge], _ms(args.start), _ms(args.end))
    print(f"loaded {len(events)} events for {symbols}")
    db = out / "replay.sqlite"
    if db.exists():
        db.unlink()
    repo = SqliteRepository(db)
    res = run_replay(cfg, specs, events, symbols, hedge_symbols=[h.upper() for h in args.hedge], repo=repo)
    repo.flush()
    repo.close()
    v = res.venue
    print(f"fills={len(res.fills)} digest={res.digest()} equity={v.positions.equity():.4f} "
          f"post_only_rejects={v.orders.post_only_rejects}")
    t = total({s: _row(v, s) for s in v.positions.positions})
    print(f"realized={t.trading:+.4f} fees={t.fees:.4f} funding={t.funding:+.4f}")
    days = sorted({dt.datetime.fromtimestamp(f.ts / 1000, dt.UTC).strftime("%Y-%m-%d") for _, f in res.fills})
    for d in days:
        print("report:", build_daily_report(db, d, out, cfg))
    if args.check_determinism:
        res2 = run_replay(cfg, specs, events, symbols, hedge_symbols=[h.upper() for h in args.hedge])
        same = res2.digest() == res.digest()
        print(f"determinism: second run digest={res2.digest()} -> {'IDENTICAL' if same else 'DIFFERENT'}")
        if not same:
            raise SystemExit(1)


def _row(v, sym):  # type: ignore[no-untyped-def]
    from riverbank.analytics.pnl import PnlRow

    p = v.positions.positions[sym]
    r = PnlRow(sym)
    r.trading = float(p.realized_pnl) + v.positions.unrealized(sym)
    r.fees, r.funding = float(p.fees), float(p.funding)
    return r


if __name__ == "__main__":
    main()
